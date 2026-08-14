-- Verifica di idoneità dei webhook Stripe reali all'emissione FatturaPA
-- (ADR-0051). SOLA LETTURA. Non stampa alcun valore: solo nomi di chiave,
-- conteggi e forme, quindi l'output non contiene dati personali.
--
-- Uso:
--   psql "$STARCHAT_DB_URL" -f scripts/verify_stripe_registry.sql
--
-- Se la colonna JSON non si chiama `payload`, sostituisci il nome nella CTE
-- `src` qui sotto: è l'unico punto da toccare.

\pset pager off

WITH src AS (
    SELECT payload AS ev            -- <<< adatta il nome colonna se serve
    FROM stripe_webhooks_registry
)

-- 1. Il payload è espanso? È LA domanda decisiva.
--    In un webhook Stripe `customer` è di norma il solo id ("cus_..."), non
--    l'oggetto. Se i dati fiscali stanno nella metadata del CUSTOMER e il
--    customer non è espanso, quei dati NON viaggiano nel webhook e il
--    connettore non potrà mai vederli.
SELECT
    'forma del riferimento customer' AS controllo,
    jsonb_typeof(ev #> '{data,object,customer}') AS forma,
    count(*) AS eventi
FROM src
WHERE ev #>> '{data,object,object}' = 'invoice'
GROUP BY 2
ORDER BY 3 DESC;

-- 2. Quali chiavi esistono nella metadata della INVOICE (l'unica sempre
--    presente nel webhook). Nomi soltanto, mai valori.
SELECT
    'chiavi metadata invoice' AS controllo,
    k AS chiave,
    count(*) AS eventi
FROM src, LATERAL jsonb_object_keys(coalesce(ev #> '{data,object,metadata}', '{}'::jsonb)) AS k
WHERE ev #>> '{data,object,object}' = 'invoice'
GROUP BY 2
ORDER BY 3 DESC;

-- 3. Le stesse chiavi sul CUSTOMER, se e quando è espanso.
SELECT
    'chiavi metadata customer (se espanso)' AS controllo,
    k AS chiave,
    count(*) AS eventi
FROM src, LATERAL jsonb_object_keys(coalesce(ev #> '{data,object,customer,metadata}', '{}'::jsonb)) AS k
GROUP BY 2
ORDER BY 3 DESC;

-- 4. Completezza fiscale sugli eventi che emetterebbero una fattura.
--    Ogni riga NULL qui è una fattura che finirebbe in quarantena.
--    Adatta i nomi di chiave se al punto 2 risultano diversi.
SELECT
    'completezza invoice.paid' AS controllo,
    count(*)                                                                        AS totale,
    count(*) FILTER (WHERE ev #>> '{data,object,metadata,vat_number}' IS NOT NULL)   AS ha_vat,
    count(*) FILTER (WHERE ev #>> '{data,object,metadata,sdi_code}'   IS NOT NULL)   AS ha_sdi,
    count(*) FILTER (WHERE ev #>> '{data,object,metadata,pec}'        IS NOT NULL)   AS ha_pec,
    count(*) FILTER (WHERE ev #>> '{data,object,customer_address,line1}'       IS NOT NULL) AS ha_indirizzo,
    count(*) FILTER (WHERE ev #>> '{data,object,customer_address,postal_code}' IS NOT NULL) AS ha_cap,
    count(*) FILTER (WHERE ev #>> '{data,object,customer_address,city}'        IS NOT NULL) AS ha_citta,
    count(*) FILTER (WHERE ev #>> '{data,object,customer_address,country}'     IS NOT NULL) AS ha_paese,
    count(*) FILTER (WHERE ev #>  '{data,object,customer_tax_ids,0}'           IS NOT NULL) AS ha_tax_id
FROM src
WHERE ev #>> '{type}' = 'invoice.paid';

-- 5. FORMA (non valore) della partita IVA in metadata: serve a sapere se
--    arriva con prefisso paese (IT0123...) o nuda. `normalize_vat` splitta il
--    prefisso, e una P.IVA nuda senza country_code esplicito genera clienti
--    duplicati. Stampa solo lunghezza e presenza di prefisso alfabetico.
SELECT
    'forma vat_number in metadata' AS controllo,
    length(ev #>> '{data,object,metadata,vat_number}')                AS lunghezza,
    (left(ev #>> '{data,object,metadata,vat_number}', 2) ~ '^[A-Za-z]{2}$') AS ha_prefisso_paese,
    count(*) AS eventi
FROM src
WHERE ev #>> '{data,object,metadata,vat_number}' IS NOT NULL
GROUP BY 2, 3
ORDER BY 4 DESC;

-- 6. Il trattamento dell'IVA sulle righe: esplicito o implicito?
--    Decide il flag `amounts_include_vat` del connettore. Se le righe non
--    portano tax_amounts, il connettore deve sapere se l'importo Stripe è
--    già IVA inclusa, altrimenti l'imponibile risulta sbagliato.
SELECT
    'iva sulle righe' AS controllo,
    count(*)                                                                  AS righe,
    count(*) FILTER (WHERE ln #> '{tax_amounts,0}' IS NOT NULL)                AS con_tax_amounts,
    count(*) FILTER (WHERE (ln #> '{tax_amounts,0,inclusive}')::text = 'true') AS iva_inclusa,
    count(DISTINCT ln #>> '{tax_amounts,0,tax_rate,percentage}')               AS aliquote_distinte
FROM src, LATERAL jsonb_array_elements(coalesce(ev #> '{data,object,lines,data}', '[]'::jsonb)) AS ln
WHERE ev #>> '{type}' = 'invoice.paid';

-- 7. Valute in gioco (una sola valuta semplifica; più valute rendono
--    load-bearing il fix sulla valuta del documento).
SELECT
    'valute' AS controllo,
    ev #>> '{data,object,currency}' AS valuta,
    count(*) AS eventi
FROM src
WHERE ev #>> '{type}' = 'invoice.paid'
GROUP BY 2
ORDER BY 3 DESC;

-- 8. Quali tipi di evento sono registrati: dice quale trigger di emissione
--    scegliere e se i rimborsi passano da credit_note o da charge.refunded.
SELECT
    'tipi di evento' AS controllo,
    ev #>> '{type}' AS tipo,
    count(*) AS eventi
FROM src
GROUP BY 2
ORDER BY 3 DESC
LIMIT 30;
