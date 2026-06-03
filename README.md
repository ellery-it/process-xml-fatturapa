# process-xml-fatturapa
removes attachments (encoded pdf),  calculate and insert &lt;Aliquota> if not present in &lt;/DatiIVA> node. Aliquota Value by guessing nearest allowed values or via articles lookup table

#processa_fatture.py
-------------------
Elabora fatture elettroniche semplificate (FSM10) in formato XML 
Inserisce aliquota IVA se non presente
Elimina allegato PDF se presente


Modalità di determinazione aliquota (in ordine di priorità):
  1. Calcolo automatico: 100 * Imposta / (Importo - Imposta)
     arrotondato al valore più vicino tra {4.00, 5.00, 10.00, 22.00}
  2. Lookup su CSV anagrafica (--anagrafica), se fornito e il codice viene trovato
     Il CSV ha precedenza sul calcolo SOLO se --preferisci-csv è specificato.
     Di default il calcolo ha sempre precedenza; il CSV è usato come fallback
     nel caso Importo == Imposta (denominatore zero).

Installazione: `pip  install lxml `

Utilizzo:

    python processa_fatture.py --input C:\\fatture_in --output C:\\fatture_out
    python processa_fatture.py --input C:\\fatture_in --output C:\\fatture_out --anagrafica articoli.csv
    python processa_fatture.py --input C:\\fatture_in --output C:\\fatture_out --anagrafica articoli.csv --preferisci-csv
    python processa_fatture.py --input C:\\fatture_in --output C:\\fatture_out --anagrafica articoli.csv --preferisci-csv --copia-saltate
    python processa_fatture.py --input C:\\fatture_in --output C:\\fatture_out --anagrafica articoli.csv --preferisci-csv --copia-saltate --rimuovi-allegati

CSV anagrafica articoli (opzionale):
    codice,aliquota
    01279,4.00
    ...
Separatore ',' o ';' rilevato automaticamente.

