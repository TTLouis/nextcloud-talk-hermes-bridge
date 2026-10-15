# Installed gateway attribution contract

This opt-in test runs the installed CLIProxyAPI 8.0.10 binary against a fixed
loopback mock. It uses no real credentials or model and must run with Docker
`--network none`, no published ports and no production configuration mounts.

Mount the installed gateway binary read-only at `/gateway`, this script at
`/test.py`, and the fork package at `/src/nextcloud_talk_hermes_bridge`. Use the
deployment's pinned Python base, UID 1000:1000, a read-only root, dropped
capabilities, `no-new-privileges`, and a writable 128 MiB `/tmp` tmpfs. Run
`python /test.py` with a 384 MiB memory limit and one CPU. The synthetic usage
output contains mock labels only.

It checks the real Connector's HTTP output, stable UUID sessions, main/project/
task parent links, readable Chinese source labels, image-generation attribution
and successful local audit receipts. It is separate from routine unit-test
discovery because it requires the installed gateway binary.
