# Signing and notarizing Wavetype for Mac

A Mac build that people download has a Developer ID signature and Apple's notarization ticket, so
it opens with a double click. Every push to `macos` builds an ad-hoc signed app instead: it is
there to be tested by the workflow, not to be handed out.

## How a release build is signed

The private key never goes to GitHub. The runner builds the app and opens a signing session
([rcodesign](https://github.com/indygreg/apple-platform-rs) remote signing); the owner's machine
joins that session with the Developer ID key and answers one signature request per file. The
files stay on the runner, the key stays on the owner's machine. Notarization is submitted from
the owner's machine too, with an App Store Connect Team API key; the runner waits for Apple's
ticket and staples it.

1. Push the commit to a new branch named `macos-release-N`:

   ```bash
   git push origin macos:macos-release-N
   ```

   Use a new `N` each time: a second push to the same branch waits for the first run to end.

2. Read the run id from the Actions page and start the owner's side right away, on the machine
   that holds the key. The session on the relay expires if nobody joins within a few minutes.
   The owner's script does four things, in this order:

   - downloads the artifact `sjs-app` and runs
     `rcodesign remote-sign --pem-file devid.key --pem-file devid.pem "<join string>"`
   - downloads `notarize-app` and runs
     `rcodesign notary-submit --api-key-file asc-key.json --wait notarize-app.dmg`
   - the same two steps for `sjs-dmg` and `notarize-dmg`

3. The workflow then staples both tickets, asks Gatekeeper (`spctl`, `stapler validate`), runs the
   test suites and the end-to-end dictation on the signed app, and opens the app from a
   quarantined copy of the .dmg with a screenshot before and after Gatekeeper's question.
   The .dmg is in the run's artifact `wavetype-mac-<run>.<attempt>`; its sha256 is at the end of
   `sign.log`.

Files: `ci/remote_sign.sh` (runner side), `devid-cert.pem` (the public certificate the session is
encrypted to), `entitlements.plist`, `build.sh dmg` (builds the .dmg from the signed app without
signing it again).

## What the owner needs, once

- A **Developer ID Application** certificate (G2 Sub-CA). Only the Account Holder can create it,
  at developer.apple.com > Certificates. The key and the request are made with OpenSSL:

  ```bash
  openssl genrsa -out devid.key 2048
  openssl req -new -key devid.key -out devid.csr -subj "/CN=Your Name/C=US"
  openssl x509 -inform DER -in developerID_application.cer -out devid.pem
  ```

  `devid.pem` is public and goes to `packaging/mac/devid-cert.pem`. `devid.key` stays in a private
  folder outside the repository.

- An **App Store Connect Team API key** (notarization takes Team keys, not individual ones),
  encoded once with `rcodesign encode-app-store-connect-api-key`.

- rcodesign 0.29.0, checked against the sha256 published with the release.

## Things that went wrong before

- Signing the `.app` folder on Windows: the bundle is full of symbolic links and Windows does not
  create them without elevated rights (WinError 1314). Hence the remote session.
- `remote-sign --p12-file`: it picked the intermediate CA instead of the leaf certificate. Pass the
  key and the certificate as two `--pem-file`.
- nightly.link serves the artifacts of a run in progress by artifact id only
  (`/actions/artifacts/<id>.zip`), not by name.

## Signing on the runner instead

`build.sh sign` also signs and notarizes by itself when `DEVID_P12_BASE64`, `DEVID_P12_PASSWORD`
and the three `APP_STORE_CONNECT_*` variables are in the environment (the list is at the top of
`build.sh`). This path puts the key on the runner; the releases so far have not used it.
