# Infomaniak Sync

Unraid-Shares mit **Infomaniak kDrive** abgleichen. In der Weboberfläche wählst du je Auftrag den ganzen Share oder einzelne Unterordner und den Zielordner auf kDrive, auch wenn der Name abweicht oder der Ordner tiefer liegt.

Die Synchronisation läuft über **rclone** und **WebDAV**, so wie Infomaniak es für kDrive beschreibt.

WebDAV ist bei kSuite Free, my kSuite und my kSuite+ nicht enthalten. Nötig ist ein kDrive mit WebDAV (zum Beispiel kDrive Solo oder kSuite Pro) und ein Anwendungspasswort.

## Unraid — Erstinstallation

Im Unraid-Terminal:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/PaulG67/infomaniak-sync/main/unraid/install-template.sh)
```

Danach in der Unraid-UI:

1. **Docker → Container hinzufügen**
2. Template **infomaniak-sync**
3. **Shares** auf `/mnt/user` lassen, wenn alle Shares sichtbar sein sollen
4. **Apply**
5. WebUI: `http://UNRAID-IP:8794`

Unter **Verbindung** wählst du entweder die vorhandene rclone-Datei oder eigene Zugangsdaten.

Die rclone-Datei ist im Template vorausgefüllt: `/boot/config/plugins/rclone/.rclone.conf`, Remote `Infomaniak` (der Eintrag `[Infomaniak]`). Die WebUI listet die weiteren WebDAV-Einträge derselben Datei. Dabei werden keine zweiten Zugangsdaten angelegt. kDrive-ID, E-Mail und Anwendungspasswort bleiben leer, solange diese Datei verwendet wird.

Eigene Zugangsdaten trägst du im Template oder unter **Verbindung** ein. Ein Speichern in der Weboberfläche hat danach Vorrang. Zurück auf die Container-Parameter geht über die Schaltfläche in der Verbindung.

Die kDrive-ID ist die Zahl nach `/drive/` in der Adresse, zum Beispiel `123456` bei `…/kdrive/app/drive/123456/`.

## Update

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/PaulG67/infomaniak-sync/main/unraid/install-template.sh)
```

Dann **Docker → infomaniak-sync → Force Update**. Neue Template-Felder zusätzlich mit **Edit → Apply**.

## Was ein Auftrag kann

- Nur hochladen, nur herunterladen, eine Seite angleichen (mit Löschen) oder beidseitig
- Vorhandene Dateien überschreiben, nur neuere übernehmen, überspringen oder die alte Fassung in `_Versionen` legen
- Beidseitig: neuere oder grössere Datei, Unraid oder kDrive gewinnt, oder beide Fassungen bleiben
- Zeitplan manuell, im Intervall, täglich, wöchentlich oder als Cron
- Probelauf, Bandbreitenlimit, Mindestalter, maximale Dateigrösse, Ausschlüsse
- Erstabgleich für den beidseitigen Modus, danach läuft der Zeitplan

Angleichen löscht Dateien auf der Zielseite. Die Oberfläche verlangt dafür eine Bestätigung. Der beidseitige Zeitplan startet erst nach einem manuellen Erstabgleich. Dabei landen überschriebene Fassungen im Versionsordner.

## Docker Compose

```yaml
services:
  infomaniak-sync:
    image: ghcr.io/paulg67/infomaniak-sync:latest
    ports:
      - "8794:8794"
    environment:
      - PUID=99
      - PGID=100
      - TZ=Europe/Zurich
      - KDRIVE_ID=123456
      - KDRIVE_USER=name@example.com
      - KDRIVE_PASSWORD=anwendungspasswort
      - RCLONE_REMOTE=Infomaniak
    volumes:
      - /mnt/user/appdata/infomaniak-sync:/config
      - /mnt/user:/shares
      - /boot/config/plugins/rclone/.rclone.conf:/rclone/rclone.conf:ro
```

## Image

`ghcr.io/paulg67/infomaniak-sync:latest`
