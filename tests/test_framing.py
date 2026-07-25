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
def test_varint_encode(ssc, n, encoded):
    assert ssc.varint(n) == encoded


@pytest.mark.parametrize("n", [0, 1, 127, 128, 300, 0xFFFFFFFF, 2 ** 56 + 3])
def test_varint_roundtrip(ssc, n):
    buf = ssc.varint(n)
    val, i = ssc.read_varint(buf, 0)
    assert (val, i) == (n, len(buf))


def test_read_varint_truncated_is_safe(ssc):
    # continuation bit set but buffer ends: must return, not loop/raise
    val, i = ssc.read_varint(b"\x80", 0)
    assert i == 1


def test_pb_parse_roundtrips_all_wire_types(ssc):
    msg = (ssc.pb_varint(1, 42)
           + ssc.pb_fixed64(2, 0xDEADBEEF00112233)
           + ssc.pb_bytes(3, b"hello")
           + ssc.pb_fixed32(4, 0x400))
    out = ssc.pb_parse(msg)
    assert out[1] == [42]
    assert struct.unpack("<Q", out[2][0])[0] == 0xDEADBEEF00112233
    assert out[3] == [b"hello"]
    assert struct.unpack("<I", out[4][0])[0] == 0x400


def test_pb_parse_repeated_fields_accumulate(ssc):
    msg = ssc.pb_bytes(2, b"a") + ssc.pb_bytes(2, b"b") + ssc.pb_bytes(2, b"c")
    assert ssc.pb_parse(msg)[2] == [b"a", b"b", b"c"]


def test_pb_parse_empty(ssc):
    assert ssc.pb_parse(b"") == {}


def test_pb_parse_unknown_wire_type_stops_cleanly(ssc):
    # wire type 3 (group start) is unsupported: parser keeps what it has
    good = ssc.pb_varint(1, 7)
    bad = ssc.tag(2, 3)
    out = ssc.pb_parse(good + bad + ssc.pb_varint(3, 9))
    assert out[1] == [7]
    assert 3 not in out


# --- QMI framing ---

def test_qmi_encode_decode_roundtrip(ssc):
    buf = ssc.qmi_encode(0x20, 7, b"hello")
    mtype, txn, msg_id, tlvs = ssc.qmi_decode(buf)
    assert (mtype, txn, msg_id) == (0, 7, 0x20)
    # requests carry the length-prefixed payload in TLV 1
    assert tlvs[1] == struct.pack("<H", 5) + b"hello"


def test_qmi_decode_short_buffer(ssc):
    assert ssc.qmi_decode(b"\x00\x01\x02") == (None, None, None, {})


def _tlv(t, data):
    return bytes([t]) + struct.pack("<H", len(data)) + data


def test_qmi_decode_walks_multiple_tlvs(ssc):
    buf = (struct.pack("<BHHH", 4, 0, 0x21, 0)
           + _tlv(1, b"\x2a")           # client id
           + _tlv(2, b"\x01\x02\x03"))  # payload
    _, _, msg_id, tlvs = ssc.qmi_decode(buf)
    assert msg_id == 0x21
    assert tlvs[1] == b"\x2a"
    assert tlvs[2] == b"\x01\x02\x03"


def test_qmi_events_reads_tlv2_behind_prefix(ssc):
    # The hard-won fact: events are protobuf field 2 of TLV 2, after a
    # 2-byte prefix; each event = fixed32 msg id (field 1) + payload
    # (field 3, optional). TLV 1 is the client id and must be ignored.
    ev_data = ssc.pb_fixed32(1, 1025) + ssc.pb_varint(2, 123) \
        + ssc.pb_bytes(3, b"\x01\x02")
    ev_bare = ssc.pb_fixed32(1, 768)          # no payload field
    ev_junk = ssc.pb_varint(2, 5)             # no msg id: skipped
    tlv2 = b"\xaa\xbb" + ssc.pb_bytes(2, ev_data) \
        + ssc.pb_bytes(2, ev_junk) + ssc.pb_bytes(2, ev_bare)
    got = list(ssc.qmi_events({1: b"\x2a", 2: tlv2}))
    assert got == [(1025, b"\x01\x02"), (768, b"")]


def test_client_request_structure(ssc):
    req = ssc.client_request((0x1111, 0x2222), 513, b"cfg")
    out = ssc.pb_parse(req)
    suid = ssc.pb_parse(out[1][0])
    assert struct.unpack("<Q", suid[1][0])[0] == 0x1111
    assert struct.unpack("<Q", suid[2][0])[0] == 0x2222
    assert struct.unpack("<I", out[2][0])[0] == 513
    assert ssc.pb_parse(out[4][0])[2] == [b"cfg"]


def test_client_request_empty_payload_sends_empty_request_field(ssc):
    out = ssc.pb_parse(ssc.client_request((1, 2), 512))
    assert out[4] == [b""]


# --- one framing module, both scripts ---

def test_scripts_share_the_framing_module(sp, rs, ssc):
    # replaces a drift test between two hand-synced copies: there is now
    # nothing to drift, and identity is what says so
    for mod in (sp, rs):
        for name in ("client_request", "qmi_encode", "qmi_decode",
                     "pb_parse", "qmi_events", "find_service"):
            assert getattr(mod, name) is getattr(ssc, name)
