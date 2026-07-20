"""Protobuf + QMI framing: the wire format decoded empirically against the
SSC. These pin down the details that cost real debugging time — most
importantly that indication *events* live in TLV 2 (TLV 1 is the client
id), behind a 2-byte prefix."""
import struct

import pytest


# --- varint / protobuf primitives ---

@pytest.mark.parametrize("n,encoded", [
    (0, b"\x00"),
    (1, b"\x01"),
    (127, b"\x7f"),
    (128, b"\x80\x01"),
    (300, b"\xac\x02"),
    (2 ** 35, b"\x80\x80\x80\x80\x80\x01"),
])
def test_varint_encode(sp, n, encoded):
    assert sp.varint(n) == encoded


@pytest.mark.parametrize("n", [0, 1, 127, 128, 300, 0xFFFFFFFF, 2 ** 56 + 3])
def test_varint_roundtrip(sp, n):
    buf = sp.varint(n)
    val, i = sp.read_varint(buf, 0)
    assert (val, i) == (n, len(buf))


def test_read_varint_truncated_is_safe(sp):
    # continuation bit set but buffer ends: must return, not loop/raise
    val, i = sp.read_varint(b"\x80", 0)
    assert i == 1


def test_pb_parse_roundtrips_all_wire_types(sp):
    msg = (sp.pb_varint(1, 42)
           + sp.pb_fixed64(2, 0xDEADBEEF00112233)
           + sp.pb_bytes(3, b"hello")
           + sp.pb_fixed32(4, 0x400))
    out = sp.pb_parse(msg)
    assert out[1] == [42]
    assert struct.unpack("<Q", out[2][0])[0] == 0xDEADBEEF00112233
    assert out[3] == [b"hello"]
    assert struct.unpack("<I", out[4][0])[0] == 0x400


def test_pb_parse_repeated_fields_accumulate(sp):
    msg = sp.pb_bytes(2, b"a") + sp.pb_bytes(2, b"b") + sp.pb_bytes(2, b"c")
    assert sp.pb_parse(msg)[2] == [b"a", b"b", b"c"]


def test_pb_parse_empty(sp):
    assert sp.pb_parse(b"") == {}


def test_pb_parse_unknown_wire_type_stops_cleanly(sp):
    # wire type 3 (group start) is unsupported: parser keeps what it has
    good = sp.pb_varint(1, 7)
    bad = sp.tag(2, 3)
    out = sp.pb_parse(good + bad + sp.pb_varint(3, 9))
    assert out[1] == [7]
    assert 3 not in out


# --- QMI framing ---

def test_qmi_encode_decode_roundtrip(sp):
    buf = sp.qmi_encode(0x20, 7, b"hello")
    mtype, txn, msg_id, tlvs = sp.qmi_decode(buf)
    assert (mtype, txn, msg_id) == (0, 7, 0x20)
    # requests carry the length-prefixed payload in TLV 1
    assert tlvs[1] == struct.pack("<H", 5) + b"hello"


def test_qmi_decode_short_buffer(sp):
    assert sp.qmi_decode(b"\x00\x01\x02") == (None, None, None, {})


def _tlv(t, data):
    return bytes([t]) + struct.pack("<H", len(data)) + data


def test_qmi_decode_walks_multiple_tlvs(sp):
    buf = (struct.pack("<BHHH", 4, 0, 0x21, 0)
           + _tlv(1, b"\x2a")           # client id
           + _tlv(2, b"\x01\x02\x03"))  # payload
    _, _, msg_id, tlvs = sp.qmi_decode(buf)
    assert msg_id == 0x21
    assert tlvs[1] == b"\x2a"
    assert tlvs[2] == b"\x01\x02\x03"


def test_qmi_events_reads_tlv2_behind_prefix(sp):
    # The hard-won fact: events are protobuf field 2 of TLV 2, after a
    # 2-byte prefix; each event = fixed32 msg id (field 1) + payload
    # (field 3, optional). TLV 1 is the client id and must be ignored.
    ev_data = sp.pb_fixed32(1, 1025) + sp.pb_varint(2, 123) \
        + sp.pb_bytes(3, b"\x01\x02")
    ev_bare = sp.pb_fixed32(1, 768)          # no payload field
    ev_junk = sp.pb_varint(2, 5)             # no msg id: skipped
    tlv2 = b"\xaa\xbb" + sp.pb_bytes(2, ev_data) \
        + sp.pb_bytes(2, ev_junk) + sp.pb_bytes(2, ev_bare)
    got = list(sp.qmi_events({1: b"\x2a", 2: tlv2}))
    assert got == [(1025, b"\x01\x02"), (768, b"")]


def test_client_request_structure(sp):
    req = sp.client_request((0x1111, 0x2222), 513, b"cfg")
    out = sp.pb_parse(req)
    suid = sp.pb_parse(out[1][0])
    assert struct.unpack("<Q", suid[1][0])[0] == 0x1111
    assert struct.unpack("<Q", suid[2][0])[0] == 0x2222
    assert struct.unpack("<I", out[2][0])[0] == 513
    assert sp.pb_parse(out[4][0])[2] == [b"cfg"]


def test_client_request_empty_payload_sends_empty_request_field(sp):
    out = sp.pb_parse(sp.client_request((1, 2), 512))
    assert out[4] == [b""]


# --- the two copies of the framing must not drift apart ---

def test_read_sensor_framing_matches_sensor_proxy(sp, rs):
    assert rs.varint(2 ** 40 + 5) == sp.varint(2 ** 40 + 5)
    assert rs.qmi_encode(0x20, 3, b"xyz") == sp.qmi_encode(0x20, 3, b"xyz")
    assert rs.client_request((7, 9), 513, b"p") \
        == sp.client_request((7, 9), 513, b"p")
    ev = sp.pb_fixed32(1, 1025) + sp.pb_bytes(3, b"\x09")
    tlvs = {2: b"\x00\x00" + sp.pb_bytes(2, ev)}
    assert list(rs.events(tlvs)) == list(sp.qmi_events(tlvs))
