# processa_fatture.py
# -------------------
# Elabora fatture elettroniche semplificate (FSM10) in formato XML 
# Inserisce aliquota IVA se non presente
# Elimina allegato PDF se presente
# (c) Andrea Diaco 15.01.2026 andreadiaco@gmail.com

# Modalità di determinazione aliquota (in ordine di priorità):
  # 1. Calcolo automatico: 100 * Imposta / (Importo - Imposta)
     # arrotondato al valore più vicino tra {4.00, 5.00, 10.00, 22.00}
  # 2. Lookup su CSV anagrafica (--anagrafica), se fornito e il codice viene trovato
     # Il CSV ha precedenza sul calcolo SOLO se --preferisci-csv è specificato.
     # Di default il calcolo ha sempre precedenza; il CSV è usato come fallback
     # nel caso Importo == Imposta (denominatore zero).

# Installazione: pip  install lxml 

# Utilizzo:
    # python processa_fatture.py --input C:\\fatture_in --output C:\\fatture_out
    # python processa_fatture.py --input C:\\fatture_in --output C:\\fatture_out --anagrafica articoli.csv
    # python processa_fatture.py --input C:\\fatture_in --output C:\\fatture_out --anagrafica articoli.csv --preferisci-csv
    # python processa_fatture.py --input C:\\fatture_in --output C:\\fatture_out --anagrafica articoli.csv --preferisci-csv --copia-saltate
    # python processa_fatture.py --input C:\\fatture_in --output C:\\fatture_out --anagrafica articoli.csv --preferisci-csv --copia-saltate --rimuovi-allegati

# CSV anagrafica articoli (opzionale):
    # codice,aliquota
    # 01279,4.00
    # ...
# Separatore ',' o ';' rilevato automaticamente.



import os
import re
import csv
import sys
import shutil
import logging
import argparse
from datetime import datetime

try:
    from lxml import etree
    USING_LXML = True
except ImportError:
    import xml.etree.ElementTree as etree
    USING_LXML = False

# ---------------------------------------------------------------------------
# Costanti
# ---------------------------------------------------------------------------

TAG_ROOT_SEMPLIFICATA_LOCAL = "FatturaElettronicaSemplificata"
RE_CODICE = re.compile(r'\[PR/(\d+)\]', re.IGNORECASE)

# Aliquote IVA ammesse (in %)
#ALIQUOTE_AMMESSE = [4.0, 5.0, 10.0, 22.0]
ALIQUOTE_AMMESSE = [4.0, 10.0, 22.0]


# ---------------------------------------------------------------------------
# Calcolo aliquota
# ---------------------------------------------------------------------------

def calcola_aliquota(importo: float, imposta: float) -> str | None:
    """
    Calcola l'aliquota IVA come 100 * Imposta / (Importo - Imposta)
    e restituisce il valore ammesso più vicino tra {4, 5, 10, 22}.
    Restituisce None se il denominatore è zero o negativo.
    """
    denominatore = importo - imposta
    if denominatore <= 0:
        return None
    aliquota_calc = 100.0 * imposta / denominatore
    # Valore ammesso più vicino
    migliore = min(ALIQUOTE_AMMESSE, key=lambda a: abs(a - aliquota_calc))
    return f"{migliore:.2f}"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_path: str) -> logging.Logger:
    logger = logging.getLogger("processa_fatture")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


# ---------------------------------------------------------------------------
# Anagrafica (opzionale)
# ---------------------------------------------------------------------------

def carica_anagrafica(csv_path: str, logger: logging.Logger) -> dict:
    """
    Legge il CSV e restituisce {codice: aliquota_str}.
    Gestisce zeri iniziali su entrambi i lati del match.
    """
    anagrafica = {}
    if not os.path.isfile(csv_path):
        logger.error(f"File anagrafica non trovato: {csv_path}")
        sys.exit(1)

    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        sample = f.read(2048)
        f.seek(0)
        sep = ';' if sample.count(';') > sample.count(',') else ','
        reader = csv.DictReader(f, delimiter=sep)
        reader.fieldnames = [h.strip().lower() for h in reader.fieldnames]

        if 'codice' not in reader.fieldnames or 'aliquota' not in reader.fieldnames:
            logger.error(
                f"Il CSV deve avere colonne 'codice' e 'aliquota'. "
                f"Trovate: {reader.fieldnames}"
            )
            sys.exit(1)

        count = 0
        for row in reader:
            codice_raw = row['codice'].strip()
            aliquota   = row['aliquota'].strip()
            if not codice_raw:
                continue
            anagrafica[codice_raw] = aliquota
            anagrafica[codice_raw.lstrip('0') or '0'] = aliquota
            count += 1

    logger.info(f"Anagrafica caricata: {count} articoli")
    return anagrafica


# ---------------------------------------------------------------------------
# Utilità XML
# ---------------------------------------------------------------------------

def is_fattura_semplificata(root) -> bool:
    tag   = root.tag
    local = tag.split('}', 1)[1] if '}' in tag else tag
    return local == TAG_ROOT_SEMPLIFICATA_LOCAL


def trova_elementi(root, local_name: str):
    """
    Cerca elementi per local name ignorando il namespace.
    Nelle fatture SDI il prefisso 'p:' è solo sul root; i figli non hanno namespace.
    """
    results = list(root.iter(local_name))
    if results:
        return results
    # Fallback: prova con namespace
    for el in root.iter():
        if '}' in el.tag:
            ns = el.tag.split('}')[0].lstrip('{')
            results = list(root.iter(f'{{{ns}}}{local_name}'))
            if results:
                return results
            break
    return []


def trova_figlio(parent, local_name: str):
    for child in parent:
        tag   = child.tag
        local = tag.split('}', 1)[1] if '}' in tag else tag
        if local == local_name:
            return child
    return None


def testo_float(el) -> float | None:
    if el is None:
        return None
    try:
        return float((el.text or "").strip().replace(',', '.'))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Elaborazione file
# ---------------------------------------------------------------------------

def elabora_file(
    xml_path: str,
    output_path: str,
    anagrafica: dict,
    preferisci_csv: bool,
    logger: logging.Logger,
    rimuovi_allegati: bool = False
) -> dict:
    stats = {"righe_ok": 0, "righe_ko": 0, "saltato": False, "motivo_salto": None, "sostituzioni_discordanti": 0, "allegati_rimossi": 0}
    filename = os.path.basename(xml_path)

    # Parse
    try:
        if USING_LXML:
            tree = etree.parse(xml_path, etree.XMLParser(remove_blank_text=False))
        else:
            tree = etree.parse(xml_path)
        root = tree.getroot()
    except Exception as e:
        logger.error(f"[{filename}] Errore parsing XML: {e}")
        stats["saltato"] = True
        stats["motivo_salto"] = "errore_parse"
        return stats

    # Filtro tipo fattura
    if not is_fattura_semplificata(root):
        logger.info(f"[{filename}] Saltato: non è una fattura semplificata.")
        stats["saltato"] = True
        stats["motivo_salto"] = "non_semplificata"
        return stats

    # Elabora ogni DatiBeniServizi
    for dbs in trova_elementi(root, "DatiBeniServizi"):
        desc_el     = trova_figlio(dbs, "Descrizione")
        importo_el  = trova_figlio(dbs, "Importo")
        dati_iva_el = trova_figlio(dbs, "DatiIVA")

        if dati_iva_el is None:
            logger.warning(f"[{filename}] DatiBeniServizi senza DatiIVA, riga ignorata.")
            stats["righe_ko"] += 1
            continue

        imposta_el = trova_figlio(dati_iva_el, "Imposta")
        importo    = testo_float(importo_el)
        imposta    = testo_float(imposta_el)

        descrizione = (desc_el.text or "").strip() if desc_el is not None else ""
        match_codice = RE_CODICE.search(descrizione)
        codice = match_codice.group(1) if match_codice else None

        # --- Determina aliquota ---
        aliquota = None
        fonte    = None

        if preferisci_csv and codice and anagrafica:
            # CSV ha precedenza
            aliquota = anagrafica.get(codice) or anagrafica.get(codice.lstrip('0') or '0')
            if aliquota:
                fonte = f"CSV (PR/{codice})"

        if aliquota is None and importo is not None and imposta is not None:
            aliquota = calcola_aliquota(importo, imposta)
            if aliquota:
                fonte = f"calcolo ({100*imposta/(importo-imposta):.2f}% → {aliquota})"

        if aliquota is None and codice and anagrafica:
            # CSV come fallback
            aliquota = anagrafica.get(codice) or anagrafica.get(codice.lstrip('0') or '0')
            if aliquota:
                fonte = f"CSV-fallback (PR/{codice})"

        if aliquota is None:
            motivo = []
            if importo is None or imposta is None:
                motivo.append("Importo/Imposta non leggibili")
            if importo is not None and imposta is not None and importo <= imposta:
                motivo.append(f"Importo ({importo}) <= Imposta ({imposta}): denominatore zero")
            if not codice:
                motivo.append("codice PR non trovato in Descrizione")
            elif anagrafica and not (anagrafica.get(codice) or anagrafica.get(codice.lstrip('0') or '0')):
                motivo.append(f"PR/{codice} non presente in anagrafica")
            logger.warning(f"[{filename}] Aliquota non determinata per '{descrizione}': {'; '.join(motivo)}")
            stats["righe_ko"] += 1
            continue

        logger.debug(f"[{filename}] PR/{codice or '?'} → aliquota {aliquota} (fonte: {fonte})")

        # Rimuovi eventuale <Aliquota> già presente
        existing = trova_figlio(dati_iva_el, "Aliquota")
        aliquota_precedente = None
        if existing is not None:
            aliquota_precedente = (existing.text or "").strip()
            dati_iva_el.remove(existing)

        # Crea <Aliquota> con stesso namespace (o nessuno) di <Imposta>
        if imposta_el is not None and '}' in imposta_el.tag:
            ns_el = imposta_el.tag.split('}')[0].lstrip('{')
            aliquota_tag = f'{{{ns_el}}}Aliquota'
        else:
            aliquota_tag = 'Aliquota'

        aliquota_el      = etree.SubElement(dati_iva_el, aliquota_tag)
        # Normalizza sempre a due decimali (es. "4" -> "4.00", "4.00" resta invariato)
        try:
            aliquota_el.text = f"{float(aliquota):.2f}"
        except (ValueError, TypeError):
            aliquota_el.text = aliquota  # fallback: scrivi così com'è

        aliquota_scritta = aliquota_el.text
        if aliquota_precedente is not None:
            if aliquota_precedente == aliquota_scritta:
                logger.debug(f"[{filename}] PR/{codice or '?'} Aliquota confermata: {aliquota_scritta} (era già {aliquota_precedente})")
            else:
                logger.warning(f"[{filename}] PR/{codice or '?'} Aliquota SOSTITUITA: {aliquota_precedente} → {aliquota_scritta} (fonte: {fonte})")
                stats["sostituzioni_discordanti"] += 1
        else:
            logger.debug(f"[{filename}] PR/{codice or '?'} Aliquota inserita: {aliquota_scritta} (fonte: {fonte})")

        # Posiziona Aliquota subito dopo Imposta
        if imposta_el is not None:
            dati_iva_el.remove(aliquota_el)
            imposta_idx = list(dati_iva_el).index(imposta_el)
            dati_iva_el.insert(imposta_idx + 1, aliquota_el)

        stats["righe_ok"] += 1

    # Rimuovi <Allegati> se richiesto
    if rimuovi_allegati:
        rimossi = 0
        for allegato in trova_elementi(root, "Allegati"):
            allegato.getparent().remove(allegato)
            rimossi += 1
        if rimossi:
            logger.debug(f"[{filename}] Rimossi {rimossi} nodi <Allegati>.")
        stats["allegati_rimossi"] = rimossi

    # Salva
    try:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        if USING_LXML:
            tree.write(output_path, xml_declaration=True, encoding='UTF-8', pretty_print=False)
        else:
            tree.write(output_path, xml_declaration=True, encoding='unicode')
    except Exception as e:
        logger.error(f"[{filename}] Errore scrittura output: {e}")
        stats["saltato"] = True

    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Processa fatture semplificate XML aggiungendo <Aliquota> in <DatiIVA>."
    )
    ap.add_argument("--input",         required=True,  help="Cartella file XML di input")
    ap.add_argument("--output",        required=True,  help="Cartella di output")
    ap.add_argument("--anagrafica",    default="",     help="CSV anagrafica articoli (opzionale)")
    ap.add_argument("--preferisci-csv",action="store_true",
                    help="Usa CSV come fonte primaria invece del calcolo automatico")
    ap.add_argument("--log",           default="",     help="Percorso log (default: <output>\\elaborazione.log)")
    ap.add_argument("--copia-saltate",  action="store_true",
                    help="Copia in output anche le fatture non semplificate (es. TD01), invariate")
    ap.add_argument("--rimuovi-allegati", action="store_true",
                    help="Rimuove il nodo <Allegati> (PDF di cortesia) da ogni fattura semplificata processata")
    args = ap.parse_args()

    os.makedirs(args.output, exist_ok=True)
    log_path = args.log or os.path.join(args.output, "elaborazione.log")
    logger   = setup_logging(log_path)

    logger.info("=" * 65)
    logger.info(f"Avvio elaborazione  {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"Libreria XML : {'lxml (veloce)' if USING_LXML else 'stdlib ElementTree'}")
    logger.info(f"Input        : {args.input}")
    logger.info(f"Output       : {args.output}")
    logger.info(f"Aliquota     : calcolo automatico (arrotond. a 4/5/10/22)"
                + (f" + CSV fallback {args.anagrafica}" if args.anagrafica else ""))
    if args.preferisci_csv and args.anagrafica:
        logger.info(f"             : CSV ha PRECEDENZA sul calcolo")
    logger.info(f"Copia saltate : {'sì' if args.copia_saltate else 'no'}")
    logger.info(f"Rimuovi allegati: {'sì' if args.rimuovi_allegati else 'no'}")
    logger.info("=" * 65)

    anagrafica = {}
    if args.anagrafica:
        anagrafica = carica_anagrafica(args.anagrafica, logger)

    if not os.path.isdir(args.input):
        logger.error(f"Cartella di input non trovata: {args.input}")
        sys.exit(1)

    xml_files = [f for f in os.listdir(args.input) if f.lower().endswith('.xml')]
    totale    = len(xml_files)
    logger.info(f"File XML trovati: {totale}")

    g_processati = g_saltati = g_righe_ok = g_righe_ko = g_disc = g_allegati = 0

    for i, filename in enumerate(xml_files, 1):
        xml_path    = os.path.join(args.input, filename)
        output_path = os.path.join(args.output, filename)

        if i % 500 == 0 or i == totale:
            logger.info(f"Progresso: {i}/{totale} ({i*100//totale}%)")

        stats = elabora_file(xml_path, output_path, anagrafica, args.preferisci_csv, logger, args.rimuovi_allegati)

        if stats["saltato"]:
            g_saltati += 1
            # Copia il file originale invariato se richiesto e il salto è per tipo (non errore parse)
            if args.copia_saltate and stats["motivo_salto"] == "non_semplificata":
                try:
                    shutil.copy2(xml_path, output_path)
                    logger.debug(f"[{filename}] Copiato invariato (non semplificata).")
                except Exception as e:
                    logger.error(f"[{filename}] Errore copia file saltato: {e}")
        else:
            g_processati += 1
            g_righe_ok   += stats["righe_ok"]
            g_righe_ko   += stats["righe_ko"]
            g_disc       += stats["sostituzioni_discordanti"]
            g_allegati   += stats["allegati_rimossi"]

    logger.info("=" * 65)
    logger.info("RIEPILOGO FINALE")
    logger.info(f"  File totali      : {totale}")
    logger.info(f"  File processati  : {g_processati}")
    logger.info(f"  File saltati     : {g_saltati}  (non semplificate o errori parse)")
    logger.info(f"  Righe OK         : {g_righe_ok}")
    logger.info(f"  Righe con errori : {g_righe_ko}")
    logger.info(f"  Aliquote discordanti: {g_disc}  (valore precedente diverso dal calcolato — cerca [WARNING] nel log)")
    if args.rimuovi_allegati:
        logger.info(f"  Allegati rimossi    : {g_allegati}")
    logger.info(f"  Log              : {log_path}")
    logger.info("=" * 65)
    print("\nElaborazione completata.")


if __name__ == "__main__":
    main()
