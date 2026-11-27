"""Bundled word lists for mock mode (offline demo without an API key)."""
from __future__ import annotations

MALE_FIRST = ["Jan", "Petr", "Jiří", "Josef", "Pavel", "Martin", "Tomáš", "Jaroslav", "Miroslav", "Zdeněk",
              "Václav", "Michal", "František", "Jakub", "Lukáš", "Milan", "David", "Karel", "Ondřej", "Vojtěch",
              "Filip", "Adam", "Radek", "Roman", "Daniel", "Marek", "Stanislav", "Vladimír", "Libor", "Matěj"]
FEMALE_FIRST = ["Jana", "Marie", "Eva", "Hana", "Anna", "Lenka", "Kateřina", "Lucie", "Věra", "Alena",
                "Petra", "Veronika", "Martina", "Jaroslava", "Tereza", "Michaela", "Zuzana", "Ivana", "Monika",
                "Helena", "Eliška", "Barbora", "Klára", "Simona", "Markéta", "Kristýna", "Adéla", "Natálie",
                "Dana", "Romana"]
MALE_LAST = ["Novák", "Svoboda", "Novotný", "Dvořák", "Černý", "Procházka", "Kučera", "Veselý", "Horák",
             "Němec", "Pokorný", "Marek", "Pospíšil", "Hájek", "Jelínek", "Král", "Růžička", "Beneš", "Fiala",
             "Sedláček", "Doležal", "Zeman", "Kolář", "Navrátil", "Čermák", "Vaněk", "Urban", "Blažek",
             "Kříž", "Kovář"]
FEMALE_LAST = ["Nováková", "Svobodová", "Novotná", "Dvořáková", "Černá", "Procházková", "Kučerová",
               "Veselá", "Horáková", "Němcová", "Pokorná", "Marková", "Pospíšilová", "Hájková", "Jelínková",
               "Králová", "Růžičková", "Benešová", "Fialová", "Sedláčková", "Doležalová", "Zemanová",
               "Kolářová", "Navrátilová", "Čermáková", "Vaňková", "Urbanová", "Blažková", "Křížová", "Kovářová"]

CITIES = [("Praha", "Praha"), ("Brno", "Jihomoravský"), ("Ostrava", "Moravskoslezský"), ("Plzeň", "Plzeňský"),
          ("Liberec", "Liberecký"), ("Olomouc", "Olomoucký"), ("České Budějovice", "Jihočeský"),
          ("Hradec Králové", "Královéhradecký"), ("Pardubice", "Pardubický"), ("Zlín", "Zlínský"),
          ("Jihlava", "Vysočina"), ("Karlovy Vary", "Karlovarský"), ("Ústí nad Labem", "Ústecký"),
          ("Kladno", "Středočeský"), ("Opava", "Moravskoslezský"), ("Frýdek-Místek", "Moravskoslezský"),
          ("Teplice", "Ústecký"), ("Havířov", "Moravskoslezský"), ("Mladá Boleslav", "Středočeský"),
          ("Prostějov", "Olomoucký")]
STORE_SUFFIX = ["Centrum", "Náměstí", "Sever", "Jih", "Park", "Nádraží", "Sídliště", "Galerie", "Západ", "Východ"]

SUPPLIER_NAMES = {
    "Pečivo": ["Pekárny Zlatý Klas s.r.o.", "Pekařství Vlnka a.s."],
    "Mléčné výrobky": ["Mlékárna Bílý Potok a.s.", "Mlékárna Údolí s.r.o."],
    "Ovoce a zelenina": ["Zelinářství Hájek & syn s.r.o.", "FreshFarm CZ s.r.o."],
    "Nápoje": ["Nápoje Pramen a.s.", "Sodovkárna Lípa s.r.o."],
    "Maso a uzeniny": ["Řeznictví Krajan s.r.o.", "Uzenářství Brod a.s."],
    "Trvanlivé potraviny": ["Spižírna Distribuce s.r.o.", "Mlýny Rovina a.s."],
    "Sladkosti a snacky": ["Cukrovinky Sladký Den s.r.o.", "Snack Factory CZ a.s."],
    "Drogerie": ["DrogaPro Velkoobchod s.r.o.", "Čistota CZ a.s."],
}

PRODUCT_WORDS = {
    "Pečivo": ["Chléb konzumní", "Rohlík tukový", "Kaiserka", "Bageta", "Chléb žitný", "Houska", "Croissant",
               "Koláč tvarohový", "Vánočka", "Chléb celozrnný", "Dalamánek", "Loupák", "Buchta makova", "Toustový chléb"],
    "Mléčné výrobky": ["Mléko polotučné", "Mléko plnotučné", "Jogurt bílý", "Jogurt jahodový", "Tvaroh měkký",
                       "Máslo", "Eidam 30%", "Gouda plátky", "Smetana ke šlehání", "Zakysaná smetana", "Kefír",
                       "Lučina", "Mozzarella", "Cottage", "Pudink vanilkový", "Řecký jogurt"],
    "Ovoce a zelenina": ["Jablka", "Banány", "Rajčata", "Okurky", "Brambory", "Cibule", "Mrkev", "Paprika",
                         "Pomeranče", "Citrony", "Hrušky", "Salát ledový", "Česnek", "Hrozny", "Jahody"],
    "Nápoje": ["Minerální voda perlivá", "Minerální voda neperlivá", "Pomerančový džus", "Cola", "Limonáda",
               "Ledový čaj", "Pivo světlé", "Víno bílé", "Tonic", "Jablečný mošt", "Energetický nápoj", "Káva mletá"],
    "Maso a uzeniny": ["Kuřecí prsa", "Vepřová krkovice", "Mleté maso", "Šunka dušená", "Salám Herkules",
                       "Párky", "Klobása", "Slanina", "Hovězí zadní", "Kuřecí stehna", "Paštika", "Tlačenka"],
    "Trvanlivé potraviny": ["Mouka hladká", "Rýže dlouhozrnná", "Těstoviny špagety", "Cukr krupice",
                            "Olej slunečnicový", "Ovesné vločky", "Čočka", "Rajčatový protlak", "Tuňák v oleji",
                            "Kečup", "Hořčice", "Sůl"],
    "Sladkosti a snacky": ["Čokoláda mléčná", "Oplatky", "Brambůrky solené", "Sušenky máslové", "Gumové bonbony",
                           "Tyčinka müsli", "Arašídy", "Perník", "Piškoty", "Popcorn"],
    "Drogerie": ["Prací gel", "Toaletní papír", "Šampon", "Sprchový gel", "Zubní pasta", "Mýdlo tekuté",
                 "Prostředek na nádobí", "Papírové kapesníky", "Deodorant", "Čistič oken"],
}
BRANDS = ["Classic", "Bio", "Premium", "Rodinné balení", "Light", "Selské", "Domácí", "Express", "Natur", "Gold"]

COMPLAINT_TEMPLATES = {
    "Nedostupnost zboží": [
        "Už {days}. den marně sháním {product} v prodejně {store}. Regál je prázdný.",
        "Dnes jsem přišel pro {product} a zase nic. Prodejna {store}, takhle to trvá už {days} dní.",
        "Proč pořád chybí {product}? V {store} jsem odešel s prázdnou.",
        "Opakovaně není k dostání {product} ({store}). Nakupuji raději jinde.",
        "Prázdné místo v regálu na {product}, obsluha neví, kdy přijde dodávka. {store}.",
        "Chtěla jsem koupit {product}, ale v prodejně {store} nebyl. Je to už po několikáté.",
        "{product} chybí v {store} minimálně {days} dní, prosím doplňte zboží.",
        "Nakupuji pravidelně v {store}, ale {product} poslední dobou téměř nikdy nemáte.",
    ],
    "Kvalita zboží": [
        "Zakoupený {product} byl nekvalitní, prosím o nápravu. Prodejna {store}.",
        "{product} z prodejny {store} měl prošlé datum spotřeby.",
        "Kvalita výrobku {product} se poslední dobou zhoršila.",
    ],
    "Cena": [
        "Cena za {product} mi přijde vysoká oproti konkurenci.",
        "U pokladny v {store} mi účtovali jinou cenu než na regálu.",
        "Akční cena na {product} nebyla v {store} uplatněna.",
    ],
    "Personál": [
        "Neochotná obsluha v prodejně {store}.",
        "Na prodejně {store} mi nikdo nebyl schopen poradit.",
        "Pokladní v {store} byla nepříjemná.",
    ],
    "Fronty u pokladen": [
        "V {store} byla otevřená jen jedna pokladna, čekal jsem 20 minut.",
        "Dlouhé fronty v {store} v odpoledních hodinách.",
        "Samoobslužné pokladny v {store} nefungují.",
    ],
    "Reklamace": [
        "Reklamuji {product}, obal byl poškozený. {store}.",
        "Chci vrátit {product}, nefunguje jak má.",
    ],
}
