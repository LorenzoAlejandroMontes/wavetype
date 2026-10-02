# Firma e notarizzazione di Wavetype per Mac (Codemagic)

Senza firma Developer ID e notarizzazione, macOS 15 blocca l'app al primo avvio e l'utente deve
passare da Impostazioni di Sistema > Privacy e sicurezza > "Apri comunque". Con firma + notarizzazione
si apre con un doppio clic. Il build di GitHub Actions (`.github/workflows/macos.yml`) resta ad-hoc:
serve a provare l'app, non a distribuirla.

## Cosa serve, una volta sola

1. **Certificato Developer ID Application (lo crea solo l'Account Holder, cioe' Lorenzo).**
   Apple non permette di crearlo con la chiave API (errore 403 "only the Account Holder"), quindi
   e' un passo a mano. Da Windows, in una cartella privata fuori dal repo:

   ```powershell
   openssl genrsa -out devid.key 2048
   openssl req -new -key devid.key -out devid.csr -subj "/CN=Lorenzo Alejandro Montes/C=IT"
   ```

   developer.apple.com > Certificates > + > **Developer ID Application** (G2 Sub-CA) > carica
   `devid.csr` > scarica il `.cer`. Poi, nella stessa cartella:

   ```powershell
   curl.exe -o DeveloperIDG2CA.cer https://www.apple.com/certificateauthority/DeveloperIDG2CA.cer
   openssl x509 -inform DER -in developerID_application.cer -out devid.pem
   openssl x509 -inform DER -in DeveloperIDG2CA.cer -out g2ca.pem
   openssl pkcs12 -export -inkey devid.key -in devid.pem -certfile g2ca.pem -out devid.p12 `
     -certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES -macalg sha1
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("$PWD\devid.p12")) | Set-Clipboard
   ```

   Le opzioni `PBE-SHA1-3DES` servono perche' `security import` di macOS non legge i .p12 col
   formato predefinito di OpenSSL 3. Il certificato intermedio G2 va dentro il .p12 perche' il
   runner non e' detto che lo abbia.

2. **Variabili su Codemagic** (le incolla Lorenzo, spuntando "Secure"): app Wavetype > Environment
   variables > gruppo `devid_signing`:
   - `DEVID_P12_BASE64` = il testo copiato sopra
   - `DEVID_P12_PASSWORD` = la password scelta in `openssl pkcs12 -export`

3. **Chiave App Store Connect**: l'integrazione `crew_app_store_connect` (chiave Team, Admin) esiste
   gia' nel team Codemagic. L'app Wavetype va aggiunta allo stesso team, altrimenti non la vede.
   notarytool accetta solo chiavi Team, non quelle individuali.

## Il workflow

`codemagic.yaml` nella radice del repo (nessun segreto dentro: arrivano dal gruppo e
dall'integrazione). Si avvia a mano da Codemagic > Start new build.

```yaml
workflows:
  macos-release:
    name: Wavetype macOS (Developer ID + notarizzazione)
    instance_type: mac_mini_m2
    max_build_duration: 90
    integrations:
      app_store_connect: crew_app_store_connect
    environment:
      groups:
        - devid_signing            # DEVID_P12_BASE64, DEVID_P12_PASSWORD
      vars:
        DEVID_IDENTITY: auto       # il primo "Developer ID Application" nel keychain
        WAVETYPE_REQUIRE_NOTARIZATION: "1"
    scripts:
      - name: Python 3.12 di python.org (Tk 8.6 incluso)
        script: |
          curl -fsSLo /tmp/python.pkg https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg
          sudo installer -pkg /tmp/python.pkg -target /
          /usr/local/bin/python3.12 -c "import tkinter; print('tk', tkinter.TkVersion)"
      - name: Chiave App Store Connect presente (conta le variabili, non le stampa)
        script: |
          n=$(env | grep -c '^APP_STORE_CONNECT_' || true)
          echo "variabili APP_STORE_CONNECT_*: $n"
          test -n "$APP_STORE_CONNECT_PRIVATE_KEY" && test -n "$APP_STORE_CONNECT_KEY_IDENTIFIER" && test -n "$APP_STORE_CONNECT_ISSUER_ID"
      - name: Build, firma, notarizzazione, DMG
        script: |
          PYTHON=/usr/local/bin/python3.12 bash packaging/mac/build.sh
          spctl -a -vv dist/Wavetype.app
          spctl -a -t open --context context:primary-signature -vv dist/Wavetype-*-arm64.dmg
    artifacts:
      - dist/*.dmg
```

`build.sh` fa tutto il resto: importa il .p12 in un keychain usa e getta, firma dentro-fuori con
hardened runtime, `--timestamp` e `entitlements.plist`, notarizza `.app` e `.dmg` con
`notarytool submit --wait` e li graffa con `stapler staple`. Se la notarizzazione e' rifiutata
stampa il log di Apple e il build fallisce.

## Se qualcosa va storto

- "variabili APP_STORE_CONNECT_*: 0": l'integrazione non espone quei nomi in questo team. Verificare
  il nome dell'integrazione in Team settings > Integrations, oppure creare il gruppo con le tre
  variabili a mano (contenuto del .p8, Key ID, Issuer ID).
- `no Developer ID Application in the keychain`: il .p12 non contiene la chiave privata o la
  password e' sbagliata.
- Notarizzazione "Invalid": il log stampato elenca i file non firmati o senza hardened runtime.

## Alternativa: GitHub Actions

Lo stesso `build.sh` firma e notarizza anche su GitHub se in Settings > Secrets and variables >
Actions ci sono `DEVID_P12_BASE64`, `DEVID_P12_PASSWORD`, `APP_STORE_CONNECT_PRIVATE_KEY`,
`APP_STORE_CONNECT_KEY_IDENTIFIER`, `APP_STORE_CONNECT_ISSUER_ID`: il workflow li passa gia'.
Il .p8 si scarica una volta sola: se non c'e' piu', serve una nuova chiave Team.
