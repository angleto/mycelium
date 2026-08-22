# Archivio dei backfill, precedente allo squash

Queste sono le migrazioni che, oltre allo schema, **trasformavano dati**
su tabelle org-scoped. Sono conservate integralmente perché lo squash
collassa la storia in una baseline che descrive lo *schema*, non le
trasformazioni: senza questi file l'intento sarebbe recuperabile solo
scavando in git, e in pratica andrebbe perso.

## Perché esiste questo archivio

Il ruolo che esegue le migrazioni è il proprietario delle tabelle. In
sviluppo e in CI è un superuser e scavalca l'RLS; **su PostgreSQL gestito
(la produzione) non lo è**. Le policy sono fail-closed su un
`app.current_org` non impostato, quindi ogni `UPDATE`/`DELETE` su una
tabella org-scoped ha toccato **zero righe senza sollevare errori**.

Il problema fu incontrato la prima volta alla `0035`/`0036` e aggirato
dentro la sola `0037`; il runner non venne sistemato, e ogni backfill
successivo ci è ricaduto. Il runner è stato corretto il 2026-08-22
(`mycelium_core.migration_rls`, vedi l'emendamento all'ADR-0015), ma quella
correzione vale per il futuro: **non riesegue ciò che non è mai girato.**

## Stato per migrazione

| Migrazione | Cosa trasformava | Stato |
|---|---|---|
Tutte verificate in produzione il 2026-08-22. Nessuna resta aperta.

| Migrazione | Cosa trasformava | Esito |
|---|---|---|
| `0011_note_parts` | `blob_sources` | **girò**: solleva `NO FORCE` attorno al proprio backfill |
| `0012_drop_notes_transcript` | `notes` | non a rischio: scrive solo nel `downgrade()` |
| `0016_drop_notes_project_id` | `note_tags` | **non verificabile, e non un difetto**: eliminava `notes.project_id` subito dopo averlo copiato, quindi la sorgente non esiste più. Restano 38 note senza tag di progetto, che sono **legali**: la `0086` rese l'invariante asimmetrico apposta (`v_projects = 0` ammesso per le note, ADR-0021) |
| `0020_checklist_note_owner` | `task_checklist_items` | non a rischio: nessuna scrittura org-scoped in `upgrade()` |
| `0022_note_link_split_atom_of` | `note_note_link` | **nessun lavoro pendente**: zero kind legacy, zero coppie non canonicalizzate, zero duplicati |
| `0023_annotations` | `comments` | **girò**: solleva `NO FORCE` attorno al proprio backfill |
| `0030_default_email_notification_pref` | — | non a rischio: le scritture stanno dentro un `CREATE FUNCTION`, girano a runtime col tenant impostato |
| `0035_reanchor_legacy_date_only_due` | `tasks` | **assorbita dalla 0037** |
| `0036_reanchor_date_only_utc_owners` | `tasks` | **assorbita dalla 0037** |
| `0037_reanchor_date_only_via_tenant_guc` | `tasks` | **girò**: imposta i GUC di tenant a mano |
| `0039_time_entry_pause` | `time_entries` | **non girò, riparata il 2026-08-22**: 48 voci chiuse avevano `accumulated_seconds` a zero (110,8 ore), riallineate a `duration_seconds`. Il dato fatturabile non era compromesso: viveva in `duration_seconds`, intatto |
| `0073_issuer_optional_legal_name` | `issuer_profiles` | non a rischio: nessuna scrittura org-scoped in `upgrade()` |
| `0086_tag_structural_invariant` | 10 tabelle | **girò**: solleva `NO FORCE` su tutte e dieci in un ciclo. È anche la migrazione il cui commento documenta l'origine del problema |
| `0087_tag_trigger_update_old_side` | — | non a rischio: le occorrenze erano nella docstring |
| `0095_invoice_dry_run` | `invoices` | **nessun lavoro pendente**: nessuna fattura ombra non marcata |
| `0099_annotation_anchor_domain` | `comments` | **non girò, riparata il 2026-08-22**: 22 annotazioni convertite al dominio `source`, 143 lasciate in `rendered` perché non più risolvibili (la migrazione prevede quel ramo apposta) |

Delle 16, quattro non avevano nessuna difesa: `0016`, `0022`, `0039`,
`0095`. Di queste solo la `0039` aveva davvero lavoro da fare. Le altre
undici o si difendevano da sole, o non scrivevano nulla di org-scoped in
`upgrade()`.

Lo script di verifica è riproducibile: interroga ogni workspace con il
contesto di tenant impostato e riporta, per ciascun backfill, quante
righe resterebbero da sistemare.

## Come rieseguirne uno

Il runner corretto stabilisce da sé il contesto: basta far girare la
logica di `upgrade()` contro il database, fuori da alembic (la revisione
risulta già applicata). La `0037` resta l'esempio di riferimento di come
si stabilisce il contesto di tenant a mano, se serve farlo caso per caso.
