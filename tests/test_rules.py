import pytest

from jobradar import rules
from jobradar.privacy import html_to_text, redact, scrub_payload


@pytest.mark.parametrize(("text", "expected"), [
    ("Salary: 75.000 - 90.000 € per year.", (75000, 90000, "year")),
    ("60.000 - 75.000 EUR. Remote-first.", (60000, 75000, "year")),
    ("Gehalt 58.000 bis 70.000 € jährlich", (58000, 70000, "year")),
    ("€80k–100k.", (80000, 100000, "year")),
    ("16 € pro Stunde", (16, 16, "hour")),
    ("ab 4.500 € brutto im Monat", (4500, 4500, "month")),
])
def test_salary(text, expected):
    s = rules.salary(text)
    assert (s["salary_min"], s["salary_max"], s["salary_period"]) == expected


def test_no_salary_in_unrelated_amounts():
    assert rules.salary("45 € Rabatt auf Ihr Jobticket") is None


@pytest.mark.parametrize(("text", "language", "expected"), [
    ("Our team works in English. No German required.", "en", "no"),
    ("German is a plus.", "en", "nice_to_have"),
    ("Fluent German required (C1).", "en", "yes"),
    ("Sehr gute Deutschkenntnisse.", "de", "yes"),
    ("Wir bieten spannende Aufgaben.", "de", "yes"),  # ad written in German
    ("We build data platforms.", "en", None),
])
def test_german(text, language, expected):
    assert rules.german_required(text, language)[0] == expected


def test_remote_and_visa_and_contract():
    assert rules.remote_mode("Hybrid: 2 days in the office", None)[0] == "hybrid"
    assert rules.remote_mode("anything", True) == ("remote", "source")
    assert rules.remote_mode("Vor Ort in Leipzig.", None)[0] == "onsite"
    assert rules.visa("We cannot sponsor visas; an EU work permit is required.", None)[0] == "no"
    assert rules.visa("Blue Card holders welcome, we sponsor your visa.", None)[0] == "yes"
    assert rules.contract_type("Unbefristete Festanstellung", [])[0] == "permanent"
    assert rules.contract_type("befristet auf 2 Jahre", [])[0] == "fixed_term"
    assert rules.contract_type("", ["freelance"]) == ("freelance", "source")


def test_skills_avoid_false_positives():
    assert "R" not in rules.skills("We work in R&D on new products.")
    assert {"Python", "SQL", "Delta Lake"} <= set(rules.skills("Python, SQL and Delta Lake"))


def test_redaction_keeps_salaries_removes_contacts():
    text, n = redact("Gehalt 58.000 bis 70.000 €. Ihr Kontakt: Max Beispiel, max@example.de\nTel. 0221 9876543")
    assert "58.000 bis 70.000" in text
    assert "Max Beispiel" not in text and "@" not in text and "9876543" not in text
    assert n >= 2


def test_scrub_payload_drops_personal_keys():
    clean = scrub_payload({"title": "Dev", "ansprechpartner": {"name": "X"}, "nested": [{"email": "a@b.c"}]})
    assert clean == {"title": "Dev", "nested": [{}]}


def test_html_to_text_lists():
    assert html_to_text("<p>Hi</p><ul><li>Python</li></ul>") == "Hi\n\n- Python"


def test_bundesagentur_v6_item():
    from jobradar.sources.bundesagentur import normalise, ref_number

    item = {  # shape of the v6 search API, as returned on 2026-10-12 (values shortened)
        "stellenangebotsTitel": "Data Engineer (m/w/d)", "arbeitszeitVollzeit": True,
        "verguetungsangabe": "JAHRESGEHALT", "gehaltsspanneVon": 54000.0, "gehaltsspanneBis": 75000.0,
        "vertragsdauer": "KEINE_ANGABE",
        "stellenlokationen": [{"adresse": {"plz": "49080", "ort": "Osnabrück", "region": "NIEDERSACHSEN",
                                           "land": "DEUTSCHLAND"}}],
        "datumErsteVeroeffentlichung": "2026-10-05", "hauptberuf": "Data Engineer", "firma": "Beispiel GmbH",
        "referenznummer": "12265-350002_JB5264323-S", "alleBerufe": ["Data Engineer"],
    }
    ad = normalise(item, None)
    assert ref_number(item) == "12265-350002_JB5264323-S"
    assert ad["title"] == "Data Engineer (m/w/d)" and ad["company"] == "Beispiel GmbH"
    assert ad["location"] == "Osnabrück, Niedersachsen" and ad["country"] == "DE"
    assert ad["job_types"] == ["VOLLZEIT"] and ad["posted_at"].year == 2026
    s = rules.salary(ad["description_raw"])
    assert (s["salary_min"], s["salary_max"], s["salary_period"]) == (54000, 75000, "year")


@pytest.mark.parametrize(("text", "language", "local", "source", "expected"), [
    ("Vi söker dig som talar flytande svenska.", "sv", "sv", None, "yes"),
    ("We build things. Swedish is a plus.", "en", "sv", None, "nice_to_have"),
    ("English is our working language. No Swedish required.", "en", "sv", None, "no"),
    ("Wij zoeken een data engineer. Vloeiend Nederlands vereist.", "nl", "nl", None, "yes"),
    ("We are looking for a data analyst. Dutch is a plus.", "en", "nl", None, "nice_to_have"),
    ("We are a growing team of data engineers.", "en", "nl", None, None),
    ("Nous recherchons un analyste. Français courant exigé.", "fr", "fr", None, "yes"),
    ("Du ska ha goda kunskaper.", "sv", "sv", "nice_to_have", "nice_to_have"),  # the source API wins
    ("Anything at all.", "en", "en", None, "no"),  # English-speaking country
    ("Anything at all.", "en", None, None, None),  # country unknown
])
def test_local_language_required(text, language, local, source, expected):
    assert rules.local_language_required(text, language, local, source)[0] == expected


def test_country_to_language():
    assert rules.local_language("DE") == "de" and rules.local_language("se") == "sv"
    assert rules.local_language("NL") == "nl" and rules.local_language("CH") is None


@pytest.mark.parametrize(("text", "expected"), [
    ("Wir suchen Sie und Ihre Erfahrung mit der Cloud für unser Team", "de"),
    ("We are looking for you to join our team and build the platform", "en"),
    ("Vi söker dig som har erfarenhet och som vill utvecklas med oss", "sv"),
    ("Wij zoeken een collega voor ons team, met ervaring in data", "nl"),
    ("Nous recherchons une personne pour rejoindre notre équipe avec les compétences", "fr"),
    ("12345", "unknown"),
])
def test_detect_language(text, expected):
    from jobradar.ingest import detect_language

    assert detect_language(text) == expected
