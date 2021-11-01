import hashlib
from pathlib import Path
import time

import pandas as pd
from sqlalchemy import text
from tldextract import extract


def normalize_url_domain(url_domain):
    _, td, tsu = extract(url_domain)
    domain = td + "." + tsu
    return domain.lower()


def load_csv(path):
    return pd.read_csv(path, comment="#")


def cached_sql_query(db, query, params=None, cache_root=Path("./data/")):
    params = params or dict()
    query_hash = hashlib.md5((query + str(params)).encode("utf8")).hexdigest()
    query_cache = cache_root / (query_hash + ".csv")
    if query_cache.exists():
        print("Found query cache:", query_cache)
        return load_csv(query_cache)
    start_time = time.time()
    if db is None:
        raise ValueError("Query not cached, a valid DB connection is required")
    result = pd.read_sql(text(query), con=db.engine, params=params)
    runtime = time.time() - start_time
    if result.empty:
        return result
    cache_root.mkdir(parents=True, exist_ok=True)
    with open(query_cache, "w+") as fd:
        fd.write(f"# Query Time: {runtime:0.4f} seconds\n")
        fd.write("#" + query.replace("\n", "\n#"))
        fd.write("\n")
        fd.write("#" + str(params).replace("\n", "\n#"))
        fd.write("\n")
        result.to_csv(fd)
    return result
