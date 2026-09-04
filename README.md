# MVM Tarifa (P1 + A1/D)

Home Assistant egyéni integráció, amely egy saját építésű, ESP8266/ESP32-alapú P1
mérőolvasó (a [jantenhove/esp8266_p1meter](https://github.com/jantenhove/esp8266_p1meter)
projekt vagy vele kompatibilis firmware) **MQTT**-n érkező adataiból élőben számolja a
villamosenergia költséget a jelenlegi **A1** (rezsicsökkentett, sávos) tarifával, és
opcionálisan az MVM bejelentett **D** (dinamikus, HUPX-alapú) tarifájával is –
összehasonlításra.

Ez a projekt a korábbi, CSV-alapú `MVM Next Energy Import` integráció leegyszerűsített,
**élő adatra épülő** utódja: nincs fájlimport, nincs feltöltés, nincs korábbi
lakó/felhasználóváltás-kezelés. Egyetlen élő fogyasztás-forrás van (a P1 mérő), és a
statisztikák **hozzáfűzve** épülnek óránként, nem újraszámolva – ez a lehető legkevesebb
terhelést jelenti a Home Assistant recorder-jének.

---

## Hogyan működik

1. Az ESP a P1-telegramból kiolvasott két fogyasztási regisztert (`consumption_low_tarif`,
   `consumption_high_tarif`, Wh, halmozott) MQTT-n publikálja.
2. Az integráció feliratkozik ezekre a topikokra, és a kettő összegéből (kWh) létrehoz egy
   normál **energia szenzort** (`sensor.mvm_tarifa_fogyasztas`) – ezt teheted az Energia
   irányítópultra forrásként, a Home Assistant recorder-je magától kezeli az előzményét.
3. **Minden óra fordulóján** (percre pontosan) az integráció megnézi, mennyit nőtt a
   fogyasztás az előző órafordulóhoz képest, és ezt az egy órányi kWh-t beárazza:
   - **A1**: a kedvezményes keretig (havi vagy éves elszámolásban, választható) a
     kedvezményes ár, felette a piaci ár.
   - **D** (ha be van kapcsolva): a kereten belül ugyanaz az A1 ár, a keret feletti rész az
     adott óra **négy negyedórájának valós, tőzsdei (HUPX) árának átlagán**.
4. Ez az egy óra **hozzáfűződik** (nem újraszámolja a teljes előzményt!) a
   `mvm_tariff:cost_a1` / `mvm_tariff:cost_d` statisztikákhoz.

Így a számítási teher **állandó** (mindig csak 1 új óra), függetlenül attól, mióta fut az
integráció – nem nő az idő múlásával, ellentétben egy „mindig mindent újraszámol" megoldással.

---

## MQTT topikok

Alapértelmezett gyökér: `sensors/power/p1meter` (a jantenhove/esp8266_p1meter
`MQTT_ROOT_TOPIC` alapértéke). Az integráció ezekre iratkozik fel:

- `<gyökér>/consumption_low_tarif` – halmozott fogyasztás, **Wh**, sima szöveges szám.
- `<gyökér>/consumption_high_tarif` – ugyanaz, a másik tarifasáv regisztere.

A kettő összege adja a teljes halmozott fogyasztást (ha a mérő csak az egyik regisztert
használja, a másik egyszerűen 0/állandó marad – nem probléma).

Ha a saját firmware-ed más topikot/gyökeret használ, a telepítéskor megadhatod.

---

## Telepítés

### HACS (egyéni repository)

1. HACS → jobb felső menü → **Custom repositories**.
2. Add hozzá a repository URL-jét, kategória: **Integration**.
3. Keresd meg és telepítsd az „MVM Tarifa (P1 + A1/D)" tételt.
4. Indítsd újra a Home Assistantot.

### Kézzel

Másold a `custom_components/mvm_tariff/` mappát a Home Assistant konfigurációs
könyvtáradba (`<config>/custom_components/mvm_tariff/`), majd indítsd újra a Home
Assistantot.

Követelmény: Home Assistant **2024.8.0** vagy újabb, **működő MQTT integráció**
(Beállítások → Eszközök és szolgáltatások → MQTT már be van állítva és csatlakozik a
brókeredhez).

---

## Beállítás

1. **Beállítások → Eszközök és szolgáltatások → Integráció hozzáadása** → „MVM Tarifa".
2. Add meg az MQTT gyökér-topikot (alapértelmezett: `sensors/power/p1meter`).
3. Az integráció „Beállítás" (Configure) gombja alatt két almenü:
   - **Áram ára (A1)** – kedvezményes/piaci ár, éves keret, havi vagy éves elszámolás.
   - **D (dinamikus) tarifa** – be-/kikapcsolás, díjtételek, EUR/HUF árfolyam
     (`0` = MNB napi árfolyam automatikusan).

---

## Entitások

| Entitás | Leírás |
|---|---|
| MVM Tarifa Fogyasztás | Élő, halmozott energia (kWh) a P1 mérőből – ez tehető az Energia dashboard forrásává. |
| MVM Tarifa Időszaki fogyasztás | A jelenlegi elszámolási időszak (hó/év) fogyasztása. |
| MVM Tarifa Kedvezményes keret | A jelenlegi időszakra érvényes kedvezményes keret. |
| MVM Tarifa Hátralévő kedvezményes keret | Mennyi van még hátra a keretből. |
| MVM Tarifa Kedvezményes keret kihasználtság | %-ban. |
| MVM Tarifa Aktuális ársáv | `kedvezményes` vagy `piaci`. |
| MVM Tarifa Becsült sávváltás | Becsült időpont, mikor lép piaci árra (az időszak eddigi átlaga alapján). |
| MVM Tarifa Összes költség (A1) | Az integráció indítása óta összesített A1 költség. |
| MVM Tarifa Összes költség (D) | Ugyanaz, D tarifával (ha be van kapcsolva). |
| MVM Tarifa D tarifa aktuális ár | Becsült aktuális bruttó D-egységár (Ft/kWh), 15 percenként frissül. Attribútuma: `forecast` (kb. 2 órával vissza, 24 órával előre, negyedóránként – lásd lent). |
| MVM Tarifa D tarifa HUPX nyers ár | Az aktuális HUPX ár Ft/kWh-ra átszámolva, díjak nélkül. Szintén van `forecast` attribútuma. |

A feltöltött statisztikák: **`mvm_tariff:cost_a1`** és **`mvm_tariff:cost_d`** (mindkettő a
Home Assistantban beállított pénznemben, halmozott összeggel) – ezekhez `statistics-graph`
kártyával csinálhatsz havi/éves bontású grafikont, vagy az Energia dashboardon adhatod meg
„Teljes költségeket követő entitás"-ként.

---

## Áram-előrejelzés (`forecast` attribútum) – automatizálásokhoz

A HUPX day-ahead árak a másnapra kb. **13:00-kor (CET)** megjelennek. A két „D tarifa"
szenzor `forecast` attribútuma ezt tartalmazza: `{start, hupx_eur_mwh, raw_huf_kwh,
gross_huf_kwh}` elemek listája, negyedóránként. Például:

```yaml
{% set fc = state_attr('sensor.mvm_tarifa_d_tarifa_aktualis_ar', 'forecast') %}
{% set cheapest = fc | sort(attribute='gross_huf_kwh') | first %}
Legolcsóbb óra: {{ as_datetime(cheapest.start) | as_local }} – {{ cheapest.gross_huf_kwh }} Ft/kWh
```

---

## Licenc

Lásd a repository licencfájlját.
