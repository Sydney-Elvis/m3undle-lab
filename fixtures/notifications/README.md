# Notification fixtures

`smtp-auth.txt` is the throwaway Mailpit credential (`mailer` / `LabNotify-1!`; Mailpit's auth file cannot carry spaces, so the
exact-password case uses the in-process fault server instead). It is copied beside the generated certificates by the suite. Nothing here is a real secret.

Pinned images and the compose wiring live in `docker-config/notifications.override.yaml`; the helpers that create the test CA,
generate the Synapse config and drive the observers live in `m3undle_lab/notifications.py`.
