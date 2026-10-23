# Freqtrade QuickScalp + Auto-Optimizer

## Deploy-Pfad (wichtig)

Docker mountet `./user_data` **relativ zum Ordner**, in dem du `docker compose` startest.

Gewünscht auf der NAS:

```text
~/Projekte/freqtrade-ki-bot
= /var/services/homes/nasadmin/Projekte/freqtrade-ki-bot
```

Nicht parallel aus `/data/projekte/freqtrade-ki-bot` starten – sonst zwei unterschiedliche Mounts.

## Einmalig: Stack auf den Projekte-Ordner umstellen

```bash
# Alten Stack unter /data stoppen (falls vorhanden)
cd /data/projekte/freqtrade-ki-bot 2>/dev/null && sudo docker compose down || true

# Rechte für Container-User (ftuser ≈ UID 1000)
cd ~/Projekte/freqtrade-ki-bot
sudo chown -R 1000:1000 user_data
sudo chmod -R u+rwX,go+rX user_data

# Stack NUR von hier starten
sudo docker compose up -d --force-recreate

# Prüfen: Mount muss auf homes/.../Projekte/... zeigen
sudo docker inspect freqtrade --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
```

Erwartung z.B.:

```text
/var/services/homes/nasadmin/Projekte/freqtrade-ki-bot/user_data -> /freqtrade/user_data
```

## Webhook

**cwd** in der Webhook-Config (nicht `/data/projekte/...`):

```text
/var/services/homes/nasadmin/Projekte/freqtrade-ki-bot
```

Befehl:

```bash
sh scripts/redeploy.sh
```

Hinweise:
- Fertige Images → **kein** `docker compose build` (sonst Webhook-Timeout, Exit **130**)
- Timeout im Webhook auf ≥ **10 Min** setzen, falls noch knapp
- Nach cwd-Umstellung einmal manuell: `cd ~/Projekte/freqtrade-ki-bot && sh scripts/redeploy.sh`

## Architektur

| Container | Rolle |
|-----------|--------|
| `freqtrade` | Paper/Live mit `QuickScalpStrategy` |
| `freqtrade-optimizer` | Parallel, schreibt nur `QuickScalpStrategy.json` |

## Workflow Laptop → NAS

1. Lokal committen + pushen  
2. Webhook: `git pull` im Projekte-Ordner + Container-Restart  
3. `.env` bleibt nur auf der NAS (nicht in Git)
