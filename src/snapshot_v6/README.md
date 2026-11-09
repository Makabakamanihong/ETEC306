# Source snapshot – software v6 (October 20, 2026), scrubbed

Read-only copy of the edge (`app/`) and cloud (`server/`) code described in the
[ETEC326 Milestone 1 detailed design](../../docs/etec326-milestone1/README.md).
It is published for review only and will not run unchanged.

Removed or replaced before publishing:

- No credentials or runtime data: `mqtt.json`, `notify.json`, `settings.json`, TLS keys and certificates, the face database (`faces/`), ONNX models, event logs, snapshots and the WhatsApp pairing QR/script are not included.
- Network addresses and account names are placeholders: `<CLOUD_SERVER_IP>`, `<PI_LAN_IP>`, `<user>`, `<pi-hostname>`.
- The SSH helper `tools/pi_run.py` and the WhatsApp pairing script are left out.
- Some comments and the original `app/README.md` are in Chinese (the working language of the lab notes).

Models (YuNet, SFace, NanoDet-Plus, palm detector) come from the OpenCV Zoo; see Appendix A of the design.
