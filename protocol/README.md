# SSC protocol schemas

The protobuf schemas the Snapdragon Sensor Core (SSC) speaks, recovered from
the Windows driver. Documentation, not build inputs: the daemon hand-rolls its
framing (the QMI/TLV layer around the payload isn't protobuf anyway).

## Re-extracting

The schemas live as `FileDescriptorProto` blobs inside `qcSensors.dll` (UMDF
sensor driver on the Windows partition — DriverStore, installed by
`qcsensors.inf`). `extract-protos.py` scrapes them out (dependency-free
descriptor parser, no protobuf library needed):

```sh
./extract-protos.py /path/to/qcSensors.dll -o .
```

Recovery is **exact for structure** — message/field/enum names, numbers, types
come verbatim from the blobs — but pinned to the DLL version pulled from
(re-extract after a driver update), and structure only, never semantics (units,
ranges, payload layouts aren't in them).

## What's committed, and what isn't

Only the two **Surface-specific** schemas (`sns_surface.proto`,
`sns_surface_imu.proto`): they exist nowhere public, recovered structure-only.

The generic `sns_*` set the extractor also produces (sns_client, sns_std,
sns_std_type, sns_std_sensor, sns_suid, sns_cal, …) is **not** committed —
Qualcomm publishes it under BSD-3-Clause-Clear in
[qualcomm/sensinghub](https://github.com/qualcomm/sensinghub) (`apis/proto/`).
Use those, or regenerate the DLL-matching set locally; recovered copies were
spot-checked against that publication (sns_suid, sns_client: identical
field-for-field).

## Transport crib sheet

QMI over QRTR to the ADSP's `SNS_CLIENT_SVC` — service 400 (a protocol
constant); node and port are discovered at runtime via the QRTR name service
(node 5, port 20 on the SL7).
Header is `<BHHH` — type, transaction, message id, length. Requests use message
id `0x20`, indications `0x21`.

| TLV | in a request | in a response | in an indication |
|-----|--------------|---------------|------------------|
| 1   | payload¹     | —             | client id        |
| 2   | —            | result        | payload¹         |
| 16  | —            | client id     | —                |

¹ The payload TLV is prefixed with a **u16 array count** before the protobuf
bytes. Omitting it yields QMI error 19; a u32 count yields error 1 (nanopb
parses the leading zero bytes as field 0).

The SUID lookup service's own SUID is
`{0xABABABABABABABAB, 0xABABABABABABABAB}`.

## Payload layouts (empirical)

`sns_std_sensor_event.data` is just `repeated float` — the schema says nothing
about what the floats mean. These layouts were **decoded empirically** (unlike
the schema structure above, which is exact); `read-sensor` is the executable
reference.

- **`sns_surface_color` event — 14 floats:** lux, CCT (K), CIE x, CIE y, R, G,
  B, raw X, raw Y, raw Z, IR, flags, atime, again. All channels are physically
  non-negative; tiny negative floats occur — clamp at 0.
- **`ambient_light` event — 2 floats:** lux (integer-quantized), raw.
