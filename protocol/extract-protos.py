#!/usr/bin/env python3
"""Recover .proto schemas embedded in a protobuf-generated binary.

C++ protobuf codegen stores each file's serialized FileDescriptorProto in the
binary. Find them by their 'name' field (tag 1, a string ending in .proto) and
decode enough of the descriptor to print messages, fields and enums.

Usage: extract-protos.py <binary> [outdir]
"""
import sys
import os

TYPES = {1: "double", 2: "float", 3: "int64", 4: "uint64", 5: "int32",
         6: "fixed64", 7: "fixed32", 8: "bool", 9: "string", 10: "group",
         11: "message", 12: "bytes", 13: "uint32", 14: "enum",
         15: "sfixed32", 16: "sfixed64", 17: "sint32", 18: "sint64"}
LABELS = {1: "optional", 2: "required", 3: "repeated"}


def varint(buf, i):
    val = shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")
    raise ValueError("truncated varint")


def fields(buf):
    """Yield (field_number, wire_type, value) until the buffer ends."""
    i = 0
    while i < len(buf):
        tag, i = varint(buf, i)
        fnum, wt = tag >> 3, tag & 7
        if fnum == 0:
            raise ValueError("field 0")
        if wt == 0:
            val, i = varint(buf, i)
        elif wt == 1:
            val, i = buf[i:i + 8], i + 8
        elif wt == 2:
            ln, i = varint(buf, i)
            if i + ln > len(buf):
                raise ValueError("truncated bytes")
            val, i = buf[i:i + ln], i + ln
        elif wt == 5:
            val, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"bad wire type {wt}")
        yield fnum, wt, val


def get(buf, want, string=False):
    out = []
    for fnum, _, val in fields(buf):
        if fnum == want:
            out.append(val.decode("utf-8", "replace") if string else val)
    return out


def render_enum(buf, indent="  "):
    name = (get(buf, 1, True) or ["?"])[0]
    lines = [f"{indent}enum {name} {{"]
    for v in get(buf, 2):
        vn = (get(v, 1, True) or ["?"])[0]
        num = next((x for f, _, x in fields(v) if f == 2), 0)
        lines.append(f"{indent}  {vn} = {num};")
    lines.append(indent + "}")
    return lines


def render_msg(buf, indent="  "):
    name = (get(buf, 1, True) or ["?"])[0]
    lines = [f"{indent}message {name} {{"]
    for f in get(buf, 2):
        fname = (get(f, 1, True) or ["?"])[0]
        num = lbl = typ = 0
        tname = None
        for fn, _, v in fields(f):
            if fn == 3:
                num = v
            elif fn == 4:
                lbl = v
            elif fn == 5:
                typ = v
            elif fn == 6:
                tname = v.decode("utf-8", "replace")
        tdesc = tname.lstrip(".") if tname else TYPES.get(typ, f"type{typ}")
        lines.append(f"{indent}  {LABELS.get(lbl,'')} {tdesc} {fname} = {num};")
    for n in get(buf, 3):
        lines += render_msg(n, indent + "  ")
    for e in get(buf, 4):
        lines += render_enum(e, indent + "  ")
    lines.append(indent + "}")
    return lines


data = open(sys.argv[1], "rb").read()
outdir = sys.argv[2] if len(sys.argv) > 2 else None
if outdir:
    os.makedirs(outdir, exist_ok=True)

seen = set()
for i in range(len(data) - 4):
    if data[i] != 0x0A:
        continue
    try:
        ln, j = varint(data, i + 1)
    except ValueError:
        continue
    if not 5 <= ln <= 120 or j + ln > len(data):
        continue
    name = data[j:j + ln]
    if not name.endswith(b".proto") or name in seen:
        continue

    # Grow the buffer until the descriptor stops parsing cleanly.
    best = None
    for size in range(ln + 2, min(len(data) - i, 262144)):
        chunk = data[i:i + size]
        try:
            list(fields(chunk))
        except ValueError:
            continue
        msgs = get(chunk, 4)
        if msgs:
            best = chunk
    if not best:
        continue
    seen.add(name)

    fname = name.decode()
    pkg = (get(best, 2, True) or [""])[0]
    out = [f'// recovered from {os.path.basename(sys.argv[1])}',
           f'// file: {fname}']
    if pkg:
        out.append(f"package {pkg};")
    out.append("")
    for m in get(best, 4):
        out += render_msg(m, "")
    for e in get(best, 5):
        out += render_enum(e, "")
    text = "\n".join(out)

    if outdir:
        with open(os.path.join(outdir, os.path.basename(fname)), "w") as fh:
            fh.write(text + "\n")
        print(f"wrote {fname} ({len(get(best,4))} messages, {len(best)} bytes)")
    else:
        print(text)

print(f"\n{len(seen)} descriptor(s) recovered", file=sys.stderr)
