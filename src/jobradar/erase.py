"""Erasure driven by lineage (GDPR Art. 17, the engineering half).

Instead of remembering by hand where a CV went, we ask the lineage log: which runs read this
CV, and what did they write? Then we delete exactly that, vacuum the Delta tables so the data
is physically gone, and record the erasure itself as a lineage event.
"""

from __future__ import annotations

from . import lineage, storage
from .config import get_settings


def find_derived(cv_hash: str) -> dict:
    """Follow lineage: runs that carry this CV hash, and every dataset they produced."""
    runs, outputs = set(), set()
    for event in lineage.read_events():
        cv_facet = event["run"].get("facets", {}).get("jobradar_cv") or {}
        if cv_facet.get("cv_hash") == cv_hash and event["eventType"] == "COMPLETE":
            runs.add(event["run"]["runId"])
            for ds in event.get("outputs", []):
                outputs.add(f'{ds["namespace"]}:{ds["name"]}')
    return {"runs": sorted(runs), "datasets": sorted(outputs)}


def erase_cv(cv_hash: str | None = None) -> dict:
    settings = get_settings()
    hashes = [cv_hash] if cv_hash else sorted({
        e["run"]["facets"]["jobradar_cv"]["cv_hash"] for e in lineage.read_events()
        if "jobradar_cv" in e["run"].get("facets", {})
    })
    report = {"cvs": [], "files_deleted": [], "rows_deleted": {}, "lineage_runs_traced": 0}

    with lineage.run("erase.cv", "Erase a CV and everything derived from it, found through lineage") as run:
        for h in hashes:
            short = h[:12]
            derived = find_derived(h)
            report["cvs"].append(short)
            report["lineage_runs_traced"] += len(derived["runs"])
            for ds in derived["datasets"]:
                namespace, name = ds.split(":", 1)
                if namespace == "jobradar-private":
                    kind = name.split("/")[0]
                    pattern = {"cv_markdown": f"cv_{short}.md", "cv_profile": f"profile_{short}.json",
                               "cv_structured": f"cvdata_{short}.json",
                               "application": name.split("/", 1)[-1] + ".json"}.get(kind)
                    if pattern:
                        for f in settings.private.glob(pattern):
                            f.unlink()
                            report["files_deleted"].append(f.name)
                elif namespace == lineage.LAKE_NAMESPACE and name == "gold.matches":
                    n = storage.delete("gold.matches", f"cv_hash = '{h}'")
                    report["rows_deleted"]["gold.matches"] = report["rows_deleted"].get("gold.matches", 0) + n
            # Belt and braces: anything left in the private folder for this hash
            for f in settings.private.glob(f"*{short}*"):
                f.unlink()
                report["files_deleted"].append(f.name)
        if not cv_hash:  # erasing everything also removes the name and contact details you typed in
            identity = settings.private / "identity.json"
            if identity.exists():
                identity.unlink()
                report["files_deleted"].append(identity.name)
        if storage.exists("gold.matches"):
            storage.vacuum("gold.matches")
            run.outputs = ["gold.matches"]
        run.run_facets["jobradar_erasure"] = lineage.custom_facet(
            "ErasureRunFacet", cv_hashes=[h[:12] for h in hashes], files_deleted=report["files_deleted"],
            rows_deleted=report["rows_deleted"], vacuumed=True,
            note="Delta files vacuumed with 0h retention: deleted rows cannot be restored by time travel.")
    return report
