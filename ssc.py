"""QMI/QRTR framing for the Qualcomm Sensor Core (SSC), and the minimal
protobuf codec it rides on.

Shared by sensor-proxy (the daemon) and read-sensor (the CLI streamer),
which each used to carry a copy kept in step by a test. Deliberately
dependency-free — no gi, no D-Bus — so 'sl7-sensors lux' keeps working
on a machine without python3-gi.

None of this is documented upstream; the wire shapes were recovered by
watching a live ADSP. Two that cost hours: indication payloads live in
TLV 2 (TLV 1 is the client_id), and a request for an unsupported rate
comes back as an error EVENT (msg 130), never a NAK.
"""
import socket
import struct
import time


AF_QIPCRTR = 42
SNS_SERVICE = 400
SNS_CLIENT_REQ = 0x20
SNS_CLIENT_REPORT_IND = 0x21
SNS_CLIENT_JUMBO_IND = 0x22
SUID_LOOKUP = (0xABABABABABABABAB, 0xABABABABABABABAB)
SNS_SUID_MSGID_REQ = 512
SNS_SUID_MSGID_EVENT = 768
MSGID_STREAM_CONFIG = 513       # sns_std_sensor_config (rate in Hz)
MSGID_SENSOR_EVENT = 1025       # sns_std_sensor_event (float data[])
MSGID_ERROR_EVENT = 130

def varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def tag(field, wire):
    return varint(field << 3 | wire)


def pb_bytes(field, data):
    return tag(field, 2) + varint(len(data)) + data


def pb_varint(field, val):
    return tag(field, 0) + varint(val)


def pb_fixed32(field, val):
    return tag(field, 5) + struct.pack("<I", val)


def pb_fixed64(field, val):
    return tag(field, 1) + struct.pack("<Q", val)


def read_varint(buf, i):
    val = shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7
    return val, i


def pb_parse(buf):
    out, i = {}, 0
    while i < len(buf):
        t, i = read_varint(buf, i)
        f, w = t >> 3, t & 7
        if w == 0:
            v, i = read_varint(buf, i)
        elif w == 1:
            v, i = buf[i:i + 8], i + 8
        elif w == 2:
            ln, i = read_varint(buf, i)
            v, i = buf[i:i + ln], i + ln
        elif w == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            break
        out.setdefault(f, []).append(v)
    return out


def qmi_encode(msg_id, txn, payload):
    val = struct.pack("<H", len(payload)) + payload
    body = b"\x01" + struct.pack("<H", len(val)) + val
    return struct.pack("<BHHH", 0, txn, msg_id, len(body)) + body


def qmi_decode(buf):
    if len(buf) < 7:
        return None, None, None, {}
    mtype, txn, msg_id, _ = struct.unpack_from("<BHHH", buf, 0)
    tlvs, i = {}, 7
    while i + 3 <= len(buf):
        t, ln = buf[i], struct.unpack_from("<H", buf, i + 1)[0]
        tlvs[t] = buf[i + 3:i + 3 + ln]
        i += 3 + ln
    return mtype, txn, msg_id, tlvs


def qmi_events(tlvs):
    """Yield (msg_id, payload) from an indication's TLV 2."""
    for e in pb_parse(tlvs[2][2:]).get(2, []):
        inner = pb_parse(e)
        if 1 not in inner:
            continue
        eid = struct.unpack("<I", inner[1][0])[0]
        yield eid, inner[3][0] if 3 in inner else b""


def client_request(suid, msg_id, payload=b""):
    suid_msg = pb_fixed64(1, suid[0]) + pb_fixed64(2, suid[1])
    susp = pb_varint(1, 1) + pb_varint(2, 0)   # APSS processor, wakeup delivery
    req = pb_bytes(2, payload) if payload else b""
    return (pb_bytes(1, suid_msg) + pb_fixed32(2, msg_id) +
            pb_bytes(3, susp) + pb_bytes(4, req))


def find_service():
    s = socket.socket(AF_QIPCRTR, socket.SOCK_DGRAM, 0)
    s.settimeout(2.0)
    s.sendto(struct.pack("<5I", 10, 0, 0, 0, 0), (1, 0xFFFFFFFE))  # NEW_LOOKUP
    found = None
    while True:
        try:
            data, _ = s.recvfrom(4096)
        except socket.timeout:
            break
        if len(data) >= 20:
            cmd, svc, _, node, port = struct.unpack_from("<5I", data, 0)
            if cmd == 4 and svc == SNS_SERVICE:
                found = (node, port)
    s.close()
    return found


def lookup_suid(addr, data_type):
    sock = socket.socket(AF_QIPCRTR, socket.SOCK_DGRAM, 0)
    sock.settimeout(3.0)
    try:
        req = client_request(SUID_LOOKUP, SNS_SUID_MSGID_REQ,
                             pb_bytes(1, data_type.encode()) + pb_varint(2, 0))
        sock.sendto(qmi_encode(SNS_CLIENT_REQ, 1, req), addr)
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            try:
                data, _ = sock.recvfrom(65536)
            except socket.timeout:
                return None
            _, _, mid, tlvs = qmi_decode(data)
            if mid not in (SNS_CLIENT_REPORT_IND, SNS_CLIENT_JUMBO_IND) \
                    or 2 not in tlvs:
                continue
            for eid, pay in qmi_events(tlvs):
                if eid != SNS_SUID_MSGID_EVENT:
                    continue
                for sb in pb_parse(pay).get(2, []):
                    su = pb_parse(sb)
                    return (struct.unpack("<Q", su[1][0])[0],
                            struct.unpack("<Q", su[2][0])[0])
        return None
    finally:
        sock.close()
