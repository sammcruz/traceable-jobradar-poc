"""Command line: jobradar <command>. Run `jobradar --help`."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from . import contracts, lineage, storage
from .config import get_settings

app = typer.Typer(help="Traceable AI pipeline over EU job ads. Local models, open formats, OpenLineage.",
                  no_args_is_help=True)
cv_app = typer.Typer(help="Load your CV (stays on this machine).", no_args_is_help=True)
lineage_app = typer.Typer(help="Inspect the lineage you own.", no_args_is_help=True)
app.add_typer(cv_app, name="cv")
app.add_typer(lineage_app, name="lineage")
console = Console()


def _progress_cb(progress: Progress, task):
    return lambda done, total: progress.update(task, completed=done, total=total)


@app.command()
def ingest(
    source: list[str] = typer.Option(None, "--source", "-s", help="arbeitnow and/or bundesagentur (default: both)"),
    days: int = typer.Option(None, help="Override the retention window from config/sources.yml"),
    from_file: Path = typer.Option(None, help="Use a saved JSON sample instead of calling the API (demos, tests)"),
):
    """Fetch job ads from the last N days into bronze and silver, then apply retention."""
    from . import ingest as ing

    settings = get_settings()
    sources = source or [s for s in ("arbeitnow", "bundesagentur") if settings.sources.get(s, {}).get("enabled")]
    for s in sources:
        fetched = None
        if from_file:
            fetched = load_sample(from_file, s)
        try:
            with console.status(f"Fetching {s}…"):
                result = ing.ingest(s, fetched=fetched, days=days)
        except Exception as exc:  # one source down must not stop the others; lineage records the FAIL
            console.print(f"[red]✗ {s} skipped:[/] {exc}")
            continue
        console.print(f"[green]✓[/] {s}: {result['ads']} ads (inserted {result['inserted']}, "
                      f"updated {result['updated']}) → silver.jobs v{result['version']}")
    deleted = ing.apply_retention(days)
    if deleted:
        console.print(f"Retention removed: {deleted}")


def load_sample(path: Path, source: str):
    """Sample files hold raw API responses; reuse each source's own normaliser."""
    from .sources import arbeitnow, bundesagentur

    raw = json.loads(path.read_text())
    if source == "arbeitnow":
        items = raw.get("arbeitnow", {}).get("data", [])
        visa = {str(i.get("slug")) for i in raw.get("arbeitnow_visa", {}).get("data", [])}
        return ([arbeitnow.normalise(i, visa) for i in items],
                [{"source": "arbeitnow", "source_id": str(i.get("slug")), "payload": i} for i in items])
    items = raw.get("bundesagentur", {}).get("stellenangebote", [])
    details = raw.get("bundesagentur_details", {})
    return ([bundesagentur.normalise(i, details.get(str(i["refnr"]))) for i in items],
            [{"source": "bundesagentur", "source_id": str(i["refnr"]),
              "payload": {"search": i, "details": details.get(str(i["refnr"]))}} for i in items])


@app.command()
def enrich(
    deep: bool = typer.Option(False, help="Also ask the local LLM to fill fields rules could not determine"),
    category: list[str] = typer.Option(None, "--category", "-c", help="Limit --deep to these category ids"),
    limit: int = typer.Option(None, help="Max ads to send to the LLM in this run"),
):
    """Classify and extract fields into gold.jobs_enriched."""
    from .enrich import run_enrich

    with Progress(console=console) as progress:
        embed_task = progress.add_task("Embedding new ads", total=None)
        task = progress.add_task("Enriching", total=None)
        result = run_enrich(deep=deep, categories=category, limit=limit, progress=_progress_cb(progress, task),
                            embed_progress=_progress_cb(progress, embed_task))
    console.print(f"[green]✓[/] {result['jobs']} jobs enriched; {result['newly_embedded']} newly embedded; "
                  f"{result['deep_enriched_now']} sent to the LLM")


@app.command()
def stats():
    """Counts per category and per extracted field."""
    if not storage.exists("gold.jobs_enriched"):
        raise typer.Exit("Run `jobradar enrich` first.")
    sql = """SELECT category_label AS category, count(*) AS jobs,
                    count(*) FILTER (WHERE german_required = 'yes') AS german_required,
                    count(*) FILTER (WHERE remote_mode = 'remote') AS remote,
                    count(*) FILTER (WHERE salary_min IS NOT NULL) AS with_salary,
                    count(*) FILTER (WHERE visa_sponsorship = 'yes') AS visa
             FROM gold_jobs_enriched GROUP BY 1 ORDER BY 2 DESC"""
    rows = storage.query(sql, ["gold.jobs_enriched"]).to_pylist()
    table = Table(*rows[0].keys()) if rows else Table("empty")
    for r in rows:
        table.add_row(*[str(v) for v in r.values()])
    console.print(table)


@cv_app.command("load")
def cv_load(path: Path = typer.Argument(..., exists=True, help="PDF, Markdown or text CV")):
    """Convert your CV to Markdown, extract a profile (no identity data) and embed it."""
    from .cv import load_cv

    with console.status("Reading your CV locally…"):
        record = load_cv(path)
    p = record["profile"]
    console.print(f"[green]✓[/] CV {record['cv_hash'][:12]} via {record['converter']}")
    console.print(f"  {p.get('headline')}\n  Skills: {', '.join(p.get('skills', [])[:15])}")


@app.command()
def match(
    category: list[str] = typer.Option(None, "--category", "-c"),
    no_german: bool = typer.Option(False, "--no-german", help="Exclude ads that require German"),
    remote: list[str] = typer.Option(None, "--remote", help="remote, hybrid and/or onsite"),
    salary: bool = typer.Option(False, "--salary", help="Only ads that state a salary"),
    visa: bool = typer.Option(False, "--visa", help="Only ads that mention visa sponsorship"),
    contract: list[str] = typer.Option(None, "--contract", help="permanent, fixed_term, freelance, …"),
    seniority: list[str] = typer.Option(None, "--seniority"),
    days: int = typer.Option(None, help="Only ads posted in the last N days"),
    top: int = typer.Option(10, help="How many to rerank with the LLM"),
    shortlist: int = typer.Option(30, help="How many to shortlist by similarity"),
    cv_hash: str = typer.Option(None, help="Which loaded CV (default: the latest)"),
):
    """Find the best-fitting ads for your CV, with reasons and gaps."""
    from .match import Filters, run_match

    filters = Filters(categories=category or [], exclude_german_required=no_german, remote_modes=remote or [],
                      salary_only=salary, visa_only=visa, contract_types=contract or [],
                      seniority=seniority or [], posted_within_days=days)
    with Progress(console=console) as progress:
        task = progress.add_task("Matching", total=None)
        results = run_match(filters, cv_hash=cv_hash, shortlist=shortlist, rerank=top,
                            progress=_progress_cb(progress, task))
    table = Table("#", "score", "sim", "title", "company", "German", "remote", "salary", "why / gaps")
    for i, r in enumerate(results[:top], 1):
        salary_txt = r.get("salary_text") or "–"
        why = "; ".join(r["reasons"][:2]) + (" | gaps: " + "; ".join(r["gaps"][:2]) if r["gaps"] else "")
        table.add_row(str(i), str(r["score"]), f"{r['similarity']:.2f}", r["title"][:45], r["company"][:25],
                      r["german_required"], r["remote_mode"], salary_txt[:20], why[:90])
    console.print(table)
    console.print(f"{len(results)} shortlisted. Links and details: `jobradar app`.")


@app.command()
def erase(
    cv_hash: str = typer.Option(None, help="Erase one CV (default: all loaded CVs)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Erase your CV and everything derived from it, found through lineage."""
    from .erase import erase_cv

    if not yes:
        typer.confirm("Delete your CV data, profile and match history permanently?", abort=True)
    report = erase_cv(cv_hash)
    console.print_json(data=report)


@app.command("retention")
def retention(days: int = typer.Option(None)):
    """Delete ads older than the retention window everywhere, then vacuum."""
    from .ingest import apply_retention

    console.print(apply_retention(days) or "Nothing to delete.")


@app.command("check-contracts")
def check_contracts_cmd():
    """Check every table against its ODCS contract. Exit code 1 on violations."""
    results = contracts.check_contracts()
    failed = False
    for name, problems in results.items():
        if problems:
            failed = True
            console.print(f"[red]✗ {name}[/]")
            for p in problems:
                console.print(f"   {p}")
        else:
            console.print(f"[green]✓ {name}[/]")
    raise typer.Exit(1 if failed else 0)


@app.command("exit-test")
def exit_test_cmd():
    """Read every table with DuckDB's own Delta reader. Proves you can leave."""
    from .exit_test import run_exit_test

    results = run_exit_test()
    for r in results:
        colour = "green" if r["status"] == "ok" else ("yellow" if r["status"] == "absent" else "red")
        console.print(f"[{colour}]{r['status']:>8}[/] {r['table']} {r['rows'] if r['rows'] is not None else ''}")
    raise typer.Exit(1 if any(r["status"].startswith("FAILED") for r in results) else 0)


@lineage_app.command("show")
def lineage_show(limit: int = typer.Option(20)):
    """Last runs from the event log: job, status, inputs → outputs."""
    events = [e for e in lineage.read_events() if e["eventType"] != "START"][-limit:]
    table = Table("time", "job", "status", "inputs", "outputs")
    for e in events:
        table.add_row(e["eventTime"][:19], e["job"]["name"], e["eventType"],
                      "\n".join(d["name"] for d in e.get("inputs", [])),
                      "\n".join(d["name"] for d in e.get("outputs", [])))
    console.print(table)


@lineage_app.command("sync")
def lineage_sync():
    """Copy the event log into the Delta table lineage.raw_events."""
    console.print(f"[green]✓[/] {lineage.sync_to_lake()} events in lineage.raw_events")


@app.command("app")
def run_app():
    """Open the local web page (Streamlit)."""
    import subprocess
    import sys

    from .config import REPO_ROOT

    subprocess.run([sys.executable, "-m", "streamlit", "run", str(REPO_ROOT / "app" / "streamlit_app.py")],
                   check=False)


@app.command()
def demo():
    """Run the whole pipeline on the bundled sample data with fake models. No network, no models."""
    from .config import REPO_ROOT

    if get_settings().provider != "fake":
        console.print("Tip: set JOBRADAR_PROVIDER=fake to run without models.")
    sample = REPO_ROOT / "tests" / "fixtures" / "sample_api_responses.json"
    ingest(source=["arbeitnow", "bundesagentur"], days=3650, from_file=sample)
    enrich(deep=True, category=None, limit=None)
    cv_load(REPO_ROOT / "tests" / "fixtures" / "sample_cv.md")
    match(category=None, no_german=False, remote=None, salary=False, visa=False, contract=None,
          seniority=None, days=None, top=5, shortlist=10, cv_hash=None)
    lineage_sync()


if __name__ == "__main__":
    app()
