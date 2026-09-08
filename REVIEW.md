# Review di affidabilità — settembre 2026

Branch: `review/reliability-fixes`. Base: `8a21192`.
Review coordinata con il repository `pycloudedge`, sullo stesso nome di branch.

## Problemi corretti

| Priorità | Problema | Correzione |
| --- | --- | --- |
| P1 | Dispositivi omonimi: il refresh risolveva nuovamente ogni dispositivo per nome, associando potenzialmente identità e dati della prima camera alle successive. Anche i comandi delle entità usavano il nome. | Riutilizzo dell'inventario già scoperto; richieste di stato per device ID; scrittura dei parametri e refresh delle entità per seriale. Eliminati anche i ripetuti download dell'intero inventario. |
| P2 | Il rinnovo ordinario della sessione manteneva il listener MQTT con le credenziali precedenti. | Arresto del listener prima dell'autenticazione e riavvio dopo il successo, anche senza `force_refresh`. |
| P2 | I callback dello stream modificavano i dizionari condivisi dai thread e pubblicavano copie ormai superate; un refresh poteva perdere eventi MQTT intervenuti durante la richiesta. | Applicazione degli aggiornamenti sul loop HA, copia del singolo dispositivo e fusione dello stato runtime subito prima di restituire i dati del refresh. |
| P2 | Uno scaricamento delle piattaforme fallito fermava comunque MQTT e stream, lasciando un'integrazione caricata ma incompleta. | Arresto dei trasporti solo dopo lo scaricamento riuscito. |
| P2 | Il primo refresh assorbiva `CancelledError`, consentendo al setup di proseguire dopo una cancellazione. | Propagazione della cancellazione. |
| P2 | Il servizio diagnostico accedeva a `last_update_success_time`, inesistente nel coordinatore HA installato. | Timestamp dell'ultimo fetch riuscito gestito dall'integrazione; associazione esplicita del config entry al coordinatore HA. |
| P2 | Gli attributi della camera esponevano `host_key`, credenziale del dispositivo non necessaria alla UI. | Rimozione dall'entità e verifica tramite API degli stati. |

Le operazioni delle entità condividono ora un unico metodo del coordinatore per scrivere i parametri. Un rifiuto dell'API genera `HomeAssistantError`, invece di apparire come un comando camera riuscito.

## Verifiche

- **20 test dell'integrazione superati**, eseguiti con le classi reali di Home Assistant 2026.9.0, Python 3.14.6 e pytest 9.1.1 nel container Colima.
- I **14 nuovi casi di regressione** falliscono sul codice originale e passano sul branch corretto.
- **111 test di pycloudedge superati**, inclusi i test esistenti di MQTT, KCP, signaling e profili video.
- Compilazione in memoria di tutti i 32 moduli sorgente dei due progetti; `git diff --check` senza errori.
- Login locale con l'account di test; due configurazioni abilitate in stato `loaded`.
- `cloudedge.get_coordinator_info` e `cloudedge.refresh_device`: HTTP 200.
- Stream reale: playlist HLS e segmento video da 21.273 byte ricevuti con HTTP 200.
- Reload delle due configurazioni abilitate riuscito; stream di test chiuso.
- Una deprecazione proveniente dal server HTTP di Home Assistant compare nei test, senza fallimenti.

Per ripetere i test completi nel container di test esistente:

```sh
docker exec homeassistant-cloudedge-test mkdir -p /tmp/cloudedge-review
docker cp custom_components homeassistant-cloudedge-test:/tmp/cloudedge-review/
docker cp tests homeassistant-cloudedge-test:/tmp/cloudedge-review/
docker exec -w /tmp/cloudedge-review -e PYTHONPATH=/tmp/cloudedge-review:/pycloudedge \
  homeassistant-cloudedge-test python -m pytest tests -q -p no:cacheprovider
```

Il container deve avere pytest e le dipendenze HA. La suite locale senza Home Assistant esegue i sei test indipendenti e salta esplicitamente il modulo del coordinatore.

## Ambiente e limiti della verifica

Il container iniziale montava `cloudedge-ha-release-v1.4.0/custom_components`. È stato ricreato usando il repository corrente, senza worktree. Nel `docker-compose.yml` locale, già ignorato da Git, la versione della libreria installata è stata allineata a `0.1.8`, evitando che HA sostituisse il codice locale con il pacchetto pubblicato. È stata verificata la corrispondenza del sorgente installato con il branch della libreria.

L'istanza espone 69 entità CloudEdge, di cui 22 indisponibili. Un account abilitato restituisce cinque dispositivi, l'altro un inventario vuoto: non è stata modificata la configurazione degli account né cancellata alcuna entità. Le altre due configurazioni erano già disabilitate.

Il controllo del browser automatico non era disponibile: il collaudo dell'istanza è avvenuto attraverso HTTP e WebSocket. Lo stream è stato verificato su una camera; qualità visiva, audio, tutte le regioni e versioni Python precedenti non sono stati collaudati. La cartella `docs/` preesistente e non tracciata è stata preservata.
