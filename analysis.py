import random
from datetime import date
from functools import lru_cache
from pathlib import Path

import rbo
import pandas as pd
from scipy import stats

from utils import cached_sql_query, load_csv


def load_high_viewership(db, data_root):
    query = """
    SELECT
        url_domain,
        MD5('lhv-' || user_id) as user_id,
        count(distinct key) as n_views
    FROM (
        select
            ftal.user_id,
            ftal.key,
            substring(ftal.url from '//(?\:www.)?([^/?$]+)[/?$]?') AS "url_domain"
        FROM "facebook-timeline_attachments:links" AS ftal
        JOIN "facebook-timeline" AS ft
            ON ft."attachments:links" = ftal.key
        WHERE
            ftal.timestamp BETWEEN extract(epoch FROM :start_date) AND extract(epoch FROM :end_date)
            AND ftal.url_domain NOT LIKE '/%'
            AND ft.is_sponsored IS FALSE
            AND url_domain IS NOT NULL
    ) as sample
    WHERE url_domain NOT IN ('facebook.com', 'instagram.com', '')
    GROUP BY (url_domain, user_id)
    """
    params = {
        "start_date": date(2021, 7, 1),
        "end_date": date(2021, 10, 1),
    }
    df = cached_sql_query(
        db, query, params=params, cache_root=data_root / "query_cache/"
    )
    return df


def load_cb_unsponsored(db, data_root):
    """
    Get the most viewed domains from cbdb during the dates of the fb
    transparency report. in addition, instead of using cbdb's url_domain
    field, we recalculate it using the same mechanism that facebook does.
    """
    query = r"""
    SELECT
        url_domain,
        count(distinct user_id) as unique_users
    FROM (
        select
            ftal.user_id,
            substring(ftal.url from '//(?\:www.)?([^/?$]+)[/?$]?') AS "url_domain"
        FROM "facebook-timeline_attachments:links" AS ftal
        JOIN "facebook-timeline" AS ft
            ON ft."attachments:links" = ftal.key
        WHERE
            ftal.timestamp BETWEEN extract(epoch FROM :start_date) AND extract(epoch FROM :end_date)
            AND ftal.url_domain NOT LIKE '/%'
            AND ft.is_sponsored IS FALSE
            AND url_domain IS NOT NULL
    ) as sample
    WHERE url_domain NOT IN ('facebook.com', 'instagram.com', '')
    GROUP BY url_domain
    ORDER BY unique_users DESC
    """
    params = {
        "start_date": date(2021, 7, 1),
        "end_date": date(2021, 10, 1),
    }
    cb = cached_sql_query(
        db, query, params=params, cache_root=data_root / "query_cache/"
    )
    cb["rank"] = list(range(1, len(cb) + 1))
    return cb


class FBCBData:
    def __init__(
        self, db=None, data_root="./data/", db_params=None, load_cb=load_cb_unsponsored
    ):
        self.db = db
        self.data_root = data_root = Path(data_root)
        self.fb = self._load_fb(data_root)
        self.cb = load_cb(self.db, data_root)
        self._load_cb = load_cb

    @staticmethod
    def filter_news_sources(df):
        news = pd.read_csv("./data/news_sources_ct.csv")
        return df.merge(news, on="url_domain")

    def high_frequency_users(self):
        return load_high_viewership(self.db, self.data_root)

    def _load_fb(self, data_root):
        fb = load_csv(data_root / "fb-transparency-q2-domains.csv")
        fb["rank"] = fb["rank"].astype("int")
        fb["unique_users"] = fb["unique_users"].astype("int")
        return fb

    @lru_cache
    def joined_domains(self, how="inner", trim=False):
        """
        Join facebook's top domains with CBDB's top domains.

        how: what type of join to do. defaults to an inner join resoling only
            in data whose domains occurs in both datasets

        trim: useful for outer joins. this will remove all domains from the
            cbdb data whose rank is lower than the lowest rank that matched the
            facebook data. this results in the smallest cbdb ranked list that
            contains all matches to the facebook data.
        """
        cb, fb = self.cb, self.fb
        cbfb = cb.merge(fb, on="url_domain", how=how, suffixes=("_cb", "_fb"))
        if trim:
            last_data_idx = pd.notna(cbfb.rank_fb).cumsum().idxmax()
            cbfb = cbfb[: last_data_idx + 1]
        return cbfb.sort_values("rank_fb").set_index("url_domain")

    def correlation_domains(self, method="kendall"):
        """
        https://mathoverflow.net/questions/15955/defining-average-rank-when-not-every-ranking-covers-the-whole-set/15958#15958
        http://polisci.usca.edu/apls301/Text/Chapter%2012.%20Significance%20and%20Measures%20of%20Association.htm
        """
        if method == "kendall":
            return self.correlation_domains_kendall()
        elif method == "rbo":
            return self.correlation_domains_rbo()
        else:
            raise ValueError(f"Invalid method type: {method}")

    def correlation_domains_kendall(self):
        cbfb = self.joined_domains().sort_values("rank_fb")
        fb = cbfb.rank_fb
        cb = cbfb.rank_cb
        return stats.kendalltau(fb, cb, nan_policy="omit", variant="c")

    def correlation_domains_rbo(self):
        cbfb = self.joined_domains(how="outer", trim=True)
        fb = cbfb.query("rank_fb > 0").sort_values("rank_fb").index.to_list()
        cb = cbfb.query("rank_cb > 0").sort_values("rank_cb").index.to_list()
        return rbo.RankingSimilarity(fb, cb).rbo_ext()

    def correlation_domains_random(self):
        cbfb = self.joined_domains(how="outer", trim=True)
        fb = cbfb.query("rank_fb >= 0").sort_values("rank_fb").rank_fb
        cb_max = cbfb.rank_cb.max()
        cb_population = list(range(1, cb_max + 1))
        while True:
            cb_cur = random.sample(cb_population, k=len(fb))
            yield stats.kendalltau(fb, cb_cur, nan_policy="omit", variant="c")

    def test_domains(self):
        """
        https://stats.stackexchange.com/questions/301990/mann-whitney-u-on-overlapping-data-sets
        """
        cbfb = self.joined_domains()
        return stats.wilcoxon(cbfb["rank_fb"], cbfb["rank_cb"])

    def test_domains_random(self):
        cbfb = self.joined_domains()
        r = random.sample(range(cbfb.rank_cb.max()), k=len(cbfb))
        return stats.wilcoxon(cbfb["rank_fb"], r)

    def correlation_views(self):
        cbfb = self.joined_domains(how="inner").sort_values("rank_fb")
        fb = cbfb.unique_users_fb
        cb = cbfb.unique_users_cb
        return stats.spearmanr(fb, cb)


if __name__ == "__main__":
    from contextlib import closing

    from citizen_browser_parsers import rds_connect

    db_params = {
        "endpoint": "citizenbrowser.cjfrb8ed0b9b.us-east-2.rds.amazonaws.com",
        "profile": "markup_prod",
    }
    with closing(rds_connect(**db_params)) as db:
        data = FBCBData(db)
    print(data.joined_domains())
    print("Domain Correlation:", data.correlation_domains())
    print("Views Correlation:", data.correlation_views())
