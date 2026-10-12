"""Generates sample_api_responses.json: invented ads in the shape of both APIs' responses.

All companies, people and ads are fictional. They exist to exercise every rule (German required or not,
remote/hybrid/onsite, salary formats, visa yes/no, contract types) and the privacy redaction.
Run: python tests/fixtures/make_sample.py
"""

import json
from datetime import UTC, datetime
from pathlib import Path

T0 = int(datetime(2026, 10, 9, 9, 0, tzinfo=UTC).timestamp())


def an(slug, title, company, desc, *, remote=False, tags=(), types=(), location="Berlin", hours_ago=0):
    return {"slug": slug, "company_name": company, "title": title, "description": desc, "remote": remote,
            "url": f"https://www.arbeitnow.com/jobs/companies/example/{slug}", "tags": list(tags),
            "job_types": list(types), "location": location, "created_at": T0 - hours_ago * 3600}


arbeitnow = [
    an("senior-data-engineer-lakehouse-1", "Senior Data Engineer (Lakehouse)", "Nordlicht Analytics GmbH",
       "<p>We build our lakehouse on <b>Delta Lake</b> and Spark with dbt and Airflow.</p>"
       "<ul><li>5+ years with Python and SQL</li><li>Experience with Databricks or Microsoft Fabric</li></ul>"
       "<p>Our team works in English. No German required.</p><p>Salary: 75.000 - 90.000 € per year.</p>"
       "<p>Hybrid: 2 days in our Berlin office. Visa sponsorship and relocation support available.</p>"
       "<p>Your contact: Jana Beispiel, jana.beispiel@nordlicht.example, +49 30 1234567</p>",
       tags=["Data", "Engineering"], types=["full time"], hours_ago=5),
    an("data-analyst-power-bi-2", "Data Analyst (m/w/d) Power BI", "Fiktiv Handel AG",
       "<p>Sie erstellen Berichte mit Power BI und SQL für unseren Vertrieb.</p>"
       "<p>Sehr gute Deutschkenntnisse (C1) und gute Englischkenntnisse.</p>"
       "<p>Unbefristete Festanstellung in Vollzeit, Homeoffice nach Absprache.</p>",
       types=["Berufserfahren"], location="Hamburg", hours_ago=20),
    an("ml-engineer-remote-3", "Machine Learning Engineer – LLM platform", "Example AI Labs",
       "<p>Fully remote within the EU. You will ship RAG systems with PyTorch, LLM serving and MLOps.</p>"
       "<p>German is a plus. €80k–100k.</p><p>We cannot sponsor visas; an EU work permit is required.</p>",
       remote=True, tags=["AI"], types=["full time"], location="Remote", hours_ago=30),
    an("frontend-developer-react-4", "Frontend Developer React/TypeScript", "Pixelwerk UG",
       "<p>React, TypeScript and Figma handoff. English-speaking team in Munich, on-site 4 days a week.</p>"
       "<p>Fixed-term contract for 18 months (befristet).</p>",
       types=["full time"], location="München", hours_ago=40),
    an("werkstudent-data-5", "Werkstudent (m/w/d) Data & Analytics", "Beispiel Mobility GmbH",
       "<p>Als Werkstudent unterstützt du unser Data-Team mit Python und SQL, 20 Stunden pro Woche.</p>"
       "<p>Gute Deutschkenntnisse sind von Vorteil. 16 € pro Stunde.</p>",
       types=["part time"], location="Köln", hours_ago=50),
    an("hr-business-partner-6", "HR Business Partner (English speaking)", "Globex Example SE",
       "<p>Partner with tech teams on recruiting, onboarding and people development.</p>"
       "<p>Working language is English. Permanent contract, full-time, hybrid in Frankfurt.</p>",
       types=["full time"], location="Frankfurt", hours_ago=60),
    an("devops-engineer-kubernetes-7", "DevOps Engineer Kubernetes & Terraform", "CloudFiktiv GmbH",
       "<p>Run Kubernetes on AWS with Terraform and CI/CD. Blue Card holders welcome, we sponsor your visa.</p>"
       "<p>60.000 - 75.000 EUR. Remote-first.</p>",
       remote=True, types=["full time"], location="Berlin", hours_ago=70),
    an("ux-designer-8", "Senior UX Designer", "Formfabrik Example",
       "<p>Lead user research and Figma prototypes for our B2B SaaS. Fluent German required (C1).</p>"
       "<p>Vor Ort in Leipzig.</p>",
       types=["full time"], location="Leipzig", hours_ago=80),
    an("freelance-data-architect-9", "Freelance Data Architect (Iceberg, Trino)", "Example Consulting",
       "<p>Freelance engagement, 6 months, 100% remote. Apache Iceberg, Trino, Kafka, data modeling.</p>",
       remote=True, types=["freelance"], location="Remote", hours_ago=90),
    an("finance-controller-10", "Controller (m/w/d)", "Musterwerke AG",
       "<p>Monatsabschlüsse, Reporting mit Excel und SAP. Fließend Deutsch. Ansprechpartnerin: Frau Erika "
       "Mustermann, Tel. 0221 9876543</p>",
       types=["Berufserfahren"], location="Stuttgart", hours_ago=100),
]

arbeitnow_visa = {"data": [arbeitnow[0], arbeitnow[6]]}


def ba(refnr, titel, beruf, arbeitgeber, ort, date):
    return {"refnr": refnr, "titel": titel, "beruf": beruf, "arbeitgeber": arbeitgeber,
            "arbeitsort": {"ort": ort, "region": "Bayern", "land": "Deutschland", "plz": "80331"},
            "aktuelleVeroeffentlichungsdatum": date, "modifikationsTimestamp": f"{date}T08:00:00.000"}


bundesagentur = {"stellenangebote": [
    ba("10000-1000000001-S", "Softwareentwickler Java (m/w/d)", "Softwareentwickler/in", "Beispiel IT GmbH",
       "München", "2026-10-08"),
    ba("10000-1000000002-S", "IT-Security Analyst (m/w/d)", "IT-Sicherheitsexperte/-expertin",
       "Sicher Example AG", "Nürnberg", "2026-10-07"),
    ba("10000-1000000003-S", "Fachinformatiker Systemintegration (m/w/d)", "Fachinformatiker/in",
       "Kommune Musterstadt", "Augsburg", "2026-10-06"),
    ba("10000-1000000004-S", "Product Owner Data Platform (m/w/d)", "Produktmanager/in", "Datenwerk Fiktiv",
       "München", "2026-10-09"),
    ba("10000-1000000005-S", "Personalreferent (m/w/d)", "Personalreferent/in", "Musterbau GmbH",
       "Regensburg", "2026-10-05"),
]}

bundesagentur_details = {
    "10000-1000000001-S": {"stellenangebotsBeschreibung":
        "Wir suchen Verstärkung für unser Team. Java, Spring, PostgreSQL und Docker. Fließende "
        "Deutschkenntnisse erforderlich. Unbefristet, Vollzeit, 2 Tage Homeoffice. Gehalt 58.000 bis 70.000 € "
        "jährlich. Ihr Kontakt: Max Beispiel, max.beispiel@example.de",
        "arbeitszeitmodelle": ["VOLLZEIT"], "ansprechpartner": {"name": "Max Beispiel"}},
    "10000-1000000002-S": {"stellenangebotsBeschreibung":
        "SIEM, Incident Response, ISO 27001. Deutsch und Englisch verhandlungssicher. Vor Ort in Nürnberg.",
        "arbeitszeitmodelle": ["VOLLZEIT"]},
    "10000-1000000003-S": {"stellenangebotsBeschreibung":
        "Betreuung von Linux- und Windows-Servern, Netzwerk und Helpdesk. Befristet auf 2 Jahre, Teilzeit möglich."},
    "10000-1000000004-S": {"stellenangebotsBeschreibung":
        "Als Product Owner verantworten Sie unsere Datenplattform (Databricks, Power BI). Scrum, Jira. "
        "Hybrides Arbeiten. Sehr gute Deutschkenntnisse."},
    "10000-1000000005-S": {"stellenangebotsBeschreibung":
        "Recruiting, Lohnbuchhaltung und Mitarbeitergespräche. Teilzeit oder Vollzeit."},
}

out = {"_note": "Invented sample data in the shape of the Arbeitnow and Bundesagentur APIs. Not real ads.",
       "arbeitnow": {"data": arbeitnow, "links": {"next": None}},
       "arbeitnow_visa": arbeitnow_visa,
       "bundesagentur": bundesagentur,
       "bundesagentur_details": bundesagentur_details}
Path(__file__).with_name("sample_api_responses.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
print("written")
