-- Tradice díla (buddhismus, islám, …) odděleně od skupiny (adresáře
-- v downloads/). Sanskrtská Abhidharmakóša je ve skupině sanskrit, ale dotaz
-- na buddhismus ji má najít — viz retrieval.scope_for_traditions().
-- NULL = dílo dědí výchozí tradice své skupiny (retrieval.GROUP_TRADITIONS),
-- takže stávající řádky není potřeba přepisovat ani znovu nahrávat.

ALTER TABLE works ADD COLUMN IF NOT EXISTS tradition text[];
