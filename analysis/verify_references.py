import json
from pathlib import Path
from urllib.request import Request, urlopen

dois = [
    "10.1890/1051-0761(1998)008[0559:NPOSWW]2.0.CO;2",
    "10.1038/s41598-018-24271-9",
    "10.5281/zenodo.18459694",
    "10.1039/C9RA04865K",
    "10.1029/2008EO100001",
    "10.1038/s41597-019-0300-6",
    "10.5194/essd-13-5483-2021",
    "10.1007/b106715",
]

rows = []
for doi in dois:
    try:
        request = Request(
            "https://doi.org/" + doi,
            headers={"Accept": "application/vnd.citationstyles.csl+json", "User-Agent": "Codex-manuscript-audit/1.0"},
        )
        with urlopen(request, timeout=30) as response:
            status = response.status
            d = json.loads(response.read().decode("utf-8"))
        rows.append({
            "doi": doi,
            "status": status,
            "title": d.get("title"),
            "authors": [" ".join(x for x in [a.get("given", ""), a.get("family", "")] if x).strip() for a in d.get("author", [])],
            "issued": d.get("issued"),
            "container": d.get("container-title"),
            "volume": d.get("volume"),
            "issue": d.get("issue"),
            "page": d.get("page"),
            "type": d.get("type"),
            "url": d.get("URL"),
        })
    except Exception as e:
        rows.append({"doi": doi, "error": str(e)})

out = Path(__file__).with_name("citation_metadata_verified.json")
out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(rows, ensure_ascii=False, indent=2))
