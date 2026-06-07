"""query/search.py 和 query/aggregate.py 单元测试。

不依赖 LLM / fastembed:
  - photo_embeddings 表空 → search_photos 自动只跑 FTS-only 路径
  - location filter / time filter / persons OR/AND 都纯 SQL
"""
from __future__ import annotations

from life_lens.query.search import search_photos, _apply_favorite_boost, _fuzzy_name_cids
from life_lens.query.aggregate import places_visited, counts_by_year


def test_search_favorite_only_filters(favorite_db):
    """favorite_only=True 只返 derived.favorite=1 的照片(生成列过滤)。"""
    r = search_photos(favorite_db, favorite_only=True)
    ids = {it["photo_id"] for it in r["items"]}
    assert ids == {"p_f1", "p_f3"}
    assert all(it["favorite"] for it in r["items"])
    assert search_photos(favorite_db, favorite_only=False)["total"] == 4


def test_search_favorite_only_with_person(favorite_db):
    """收藏 + 人物组合:张三的收藏只有 p_f1(p_f2 张三但非收藏)。"""
    r = search_photos(favorite_db, persons=["张三"], favorite_only=True)
    assert {it["photo_id"] for it in r["items"]} == {"p_f1"}


def test_favorite_boost_is_light():
    """收藏轻加权:末位收藏上浮约 bonus 位,不置顶、非收藏相对顺序不乱。"""
    items = [{"photo_id": str(i), "favorite": (i == 5)} for i in range(6)]
    ids = [it["photo_id"] for it in _apply_favorite_boost(items, bonus=3)]
    assert ids == ["0", "1", "2", "5", "3", "4"]


def test_search_photos_no_query_returns_all(populated_db):
    """无 query 时返回全部(只过滤 source != seed)。"""
    r = search_photos(populated_db)
    assert r["total"] == 5
    assert len(r["items"]) == 5


def test_search_photos_query_matches_description(populated_db):
    """query='海边' 命中 description 含'海边'的 3 张(p_003, p_005, 还有'夕阳下的海边')。"""
    r = search_photos(populated_db, query="海边")
    # trigram FTS 至少能命中 p_003, p_005;p_004 没有"海边"
    ids = {item["photo_id"] for item in r["items"]}
    assert "p_003" in ids
    assert "p_005" in ids
    assert "p_004" not in ids   # 李丽公园,不应命中海边


def test_search_photos_persons_or(populated_db):
    """persons=['张三'] OR mode 返回所有含张三的 3 张。"""
    r = search_photos(populated_db, persons=["张三"])
    ids = {item["photo_id"] for item in r["items"]}
    assert ids == {"p_001", "p_002", "p_003"}


def test_search_photos_persons_and(populated_db):
    """persons=['张三','李丽'] AND mode 只返回同时含两人的 p_003。"""
    r = search_photos(populated_db, persons=["张三", "李丽"], persons_mode="AND")
    ids = {item["photo_id"] for item in r["items"]}
    assert ids == {"p_003"}


def test_search_photos_persons_and_unrecognized(populated_db):
    """AND 模式遇未识别人名严格返 0(不退化成 OR)。"""
    r = search_photos(populated_db, persons=["张三", "完全不存在的人"], persons_mode="AND")
    assert len(r["items"]) == 0


# ---------- 人名模糊匹配(精确没中时 ≥2 字互为子串兜底) ----------

def test_fuzzy_name_cids_partial_name():
    """库里 3 字全名,用户只说后 2 字 → 命中(主场景)。"""
    m = {"王小明": ["seed_a", "c_1"], "李丽": ["seed_b"]}
    assert _fuzzy_name_cids(m, "小明") == ["seed_a", "c_1"]


def test_fuzzy_name_cids_reverse_containment():
    """用户带姓+后缀("王小明小朋友"),库内名是其子串 → 也命中。"""
    m = {"王小明": ["seed_a"]}
    assert _fuzzy_name_cids(m, "王小明小朋友") == ["seed_a"]


def test_fuzzy_name_cids_single_char_blocked():
    """单字太泛,不模糊(空列表)。"""
    m = {"王小明": ["seed_a"], "李明": ["seed_b"]}
    assert _fuzzy_name_cids(m, "明") == []


def test_fuzzy_name_cids_multi_hit_returns_all():
    """模糊命中多个 name 时全部返回(OR 语义)。"""
    m = {"王小明": ["seed_a"], "张小明": ["seed_c"]}
    assert set(_fuzzy_name_cids(m, "小明")) == {"seed_a", "seed_c"}


def test_search_photos_persons_fuzzy_or(populated_db):
    """集成:persons=['张三丰'] 精确没中 → 模糊兜底命中'张三'(nm in p 方向)。"""
    r = search_photos(populated_db, persons=["张三丰"])
    assert {it["photo_id"] for it in r["items"]} == {"p_001", "p_002", "p_003"}


def test_search_photos_persons_fuzzy_and(populated_db):
    """集成:AND 模式下模糊人名也参与分组,不再被当 unrecognized 清零。"""
    r = search_photos(populated_db, persons=["张三丰", "李丽"], persons_mode="AND")
    assert {it["photo_id"] for it in r["items"]} == {"p_003"}


def test_search_photos_time_window(populated_db):
    """time_from 过滤(fixture 都是 2026-01-15,改个未来时间应该 0 结果)。"""
    r = search_photos(populated_db, time_from="2099-01-01")
    assert r["total"] == 0


def test_search_photos_time_to_day_only_fix(populated_db):
    """time_to 只到日期(YYYY-MM-DD)时,后端应自动补 T23:59:59 避免字典序漏当天。
    fixture 照片 captured_at_local = '2026-01-15T12:00:00',
    传 time_to='2026-01-15' 应该包含它。"""
    r = search_photos(populated_db, time_to="2026-01-15")
    assert r["total"] > 0   # 至少有 fixture 5 张


def test_places_visited_basic(populated_db):
    """places_visited 按 place_name 聚合 (公园 2 张 / 海边 2 张 / 城市 1 张)。"""
    r = places_visited(populated_db)
    places = {p["formatted_address"]: p["count"] for p in r["places"]}
    assert places.get("公园") == 2
    assert places.get("海边") == 2
    assert places.get("城市") == 1


def test_places_visited_persons_and_unrecognized_strict(populated_db):
    """AND 模式 + 不存在的人 → places 空 + unrecognized_persons 列出来。"""
    r = places_visited(populated_db, persons=["张三", "完全不存在"], persons_mode="AND")
    assert r["places"] == []
    assert "完全不存在" in r.get("unrecognized_persons", [])


def test_counts_by_year(populated_db):
    """counts_by_year 按年聚合。fixture 全是 2026。"""
    r = counts_by_year(populated_db)
    years = {row["year"]: row["count"] for row in r["by_year"]}
    assert years.get(2026) == 5
