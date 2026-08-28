# D:\AllCode\project\Python\M3U\HI-C-iptv-playlist\scripts\build.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import re
import time
import logging
import shutil
import subprocess
from collections import defaultdict, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

requests.packages.urllib3.disable_warnings()

# ================== 路径配置 ==================
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
OUTPUT_FILE = BASE_DIR / "playlist.m3u"
LOG_DIR = BASE_DIR / "logs"
DEAD_SOURCES_LOG = LOG_DIR / "dead_sources.log"
DEAD_SOURCES_SUMMARY = LOG_DIR / "dead_sources_summary.txt"

# ================== 日志配置 ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("HI-C-iptv-playlist")


# ================== 工具函数 ==================
def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_text(path):
    if not path.exists():
        return ""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def normalize_name(name):
    return re.sub(r"[^\w\u4e00-\u9fff]", "", name).lower()


def log_dead_source(source_type, source, reason):
    """记录失效的本地源文件或网络源 URL，同时写入汇总文件（去重）"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

        # 1. 写入 operational log（每周清空）
        with open(DEAD_SOURCES_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] [{source_type}] {source} | 原因: {reason}\n")

        # 2. 写入 summary（长期累积，去重）
        _append_dead_source_summary(source_type, source, reason)
    except Exception as e:
        logger.warning(f"写入失效源日志失败: {e}")


def _append_dead_source_summary(source_type, source, reason):
    """将失效源写入汇总文件，已存在的源不再重复写入"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        # 检查是否已存在
        if DEAD_SOURCES_SUMMARY.exists():
            with open(DEAD_SOURCES_SUMMARY, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and source in line:
                        return

        # 首次出现，追加
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        is_new_file = not DEAD_SOURCES_SUMMARY.exists() or DEAD_SOURCES_SUMMARY.stat().st_size == 0
        with open(DEAD_SOURCES_SUMMARY, "a", encoding="utf-8") as f:
            if is_new_file:
                f.write("# 失效源汇总（长期保留，手动清理）\n")
                f.write("# 格式: 首次发现时间 | 类型 | 源 | 原因\n\n")
            f.write(f"{timestamp} | {source_type} | {source} | {reason}\n")
    except Exception as e:
        logger.warning(f"写入失效源汇总失败: {e}")


def load_dead_sources(path):
    """读取历史失效源记录，返回需要跳过的源标识集合"""
    skip_set = set()
    if not path.exists():
        return skip_set
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # 格式: [timestamp] [type] source | 原因: reason
                match = re.search(r"\[[^\]]+\]\s*(.+?)\s*\|\s*原因:", line)
                if match:
                    skip_set.add(match.group(1).strip())
    except Exception as e:
        logger.warning(f"读取失效源日志失败: {e}")
    return skip_set

def get_session():
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=0.5)
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


# ================== 分组关键词 ==================
DEFAULT_GROUP_KEYWORDS = {
    "央视": ["cctv", "cgtn", "央视", "中央"],
    "卫视": ["卫视"],
    "港澳": ["香港", "澳门", "tvb", "翡翠", "明珠", "凤凰", "viutv", "now", "澳视", "有线", "澳亚", "澳門", "macau"],
    "台": ["台视", "民视", "三立", "tvbs", "中天", "东森", "华视", "中视", "公视", "大爱", "纬来", "八大", "台湾", "tw"],
    "日本": ["nhk", "tbs", "fuji", "ntv", "tv asahi", "abema", "tokyo", "日本", "japan"],
    "韩国": ["kbs", "mbc", "sbs", "jtbc", "tvn", "韩国", "korea", "korean"],
    "美国": ["cnn", "fox", "nbc", "cbs", "abc", "msnbc", "usa", "美国", "america", "hbo", "espn"],
    "英国": ["bbc", "sky", "itv", "channel 4", "channel 5", "英国", "britain", "uk"],
    "欧洲": ["france", "euronews", "dw", "德国", "法国", "意大利", "欧洲", "europe"],
    "国际": ["al jazeera", "rt ", "russia today", "discovery", "national geographic", "国际", "international"],
}

# 默认城市关键词，可在 sources.json 的 city_keywords 中扩展
DEFAULT_CITY_KEYWORDS = {
    "无锡": ["无锡"],
    "苏州": ["苏州"],
    "上海": ["上海", "shanghai"],
    "杭州": ["杭州", "hangzhou"],
    "北京": ["北京", "beijing"],
    "南京": ["南京", "nanjing"],
    "四川": ["四川", "sichuan"],
}


def classify_channel(name, group_keywords, city_keywords=None):
    """按名称判断频道分组，优先级：央视 > 卫视 > 城市 > 港澳/台/日本/韩国/美国/英国/欧洲 > 国际"""
    n = name.lower()
    n_norm = normalize_name(name)

    # 1. 央视
    for kw in group_keywords.get("央视", []):
        if kw.lower() in n:
            return "央视"

    # 2. 卫视（必须在城市之前判断）
    for kw in group_keywords.get("卫视", []):
        if kw.lower() in n:
            return "卫视"

    # 3. 城市
    if city_keywords:
        for city, kws in city_keywords.items():
            for kw in kws:
                if kw.lower() in n or normalize_name(kw) in n_norm:
                    return "城市"

    # 4. 其他分组
    order = ["港澳", "台", "日本", "韩国", "美国", "英国", "欧洲", "国际"]
    for group in order:
        for kw in group_keywords.get(group, []):
            if kw.lower() in n:
                return group

    return "国际"


# ================== 解析器 ==================
def parse_m3u(content):
    channels = []
    current_name = None
    current_info = None
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            current_info = line
            if "," in line:
                current_name = line.rsplit(",", 1)[-1].strip()
            else:
                current_name = "Unknown"
            # 优先使用 tvg-name
            match = re.search(r'tvg-name="([^"]+)"', line)
            if match:
                current_name = match.group(1).strip()
        elif line.startswith("http") and current_name:
            channels.append((current_name, line.strip(), current_info))
            current_name = None
            current_info = None
    return channels


def parse_txt(content):
    channels = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "#genre#" in line:
            continue
        if "," in line:
            parts = line.rsplit(",", 1)
            name = parts[0].strip()
            url = parts[1].strip()
            if url.startswith("http"):
                channels.append((name, url, None))
    return channels


def parse_source(content):
    if "#EXTM3U" in content:
        return parse_m3u(content)
    return parse_txt(content)


# ================== 下载 ==================
def download_text(url, timeout=20):
    try:
        import ssl
        import urllib.request
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        logger.warning(f"下载失败: {url}, 错误: {e}")
        return ""


# ================== 配置解析 ==================
def parse_alias(text):
    alias_map = {}
    regex_aliases = []

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # 忽略 iptv-master 中带 * 号的黑名单式别名
        if line.startswith("*"):
            continue

        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue

        std_name = parts[0]
        for raw in parts[1:]:
            if not raw:
                continue
            if raw.startswith("re:"):
                pattern = raw[3:].strip()
                try:
                    regex_aliases.append((std_name, re.compile(pattern)))
                except re.error as e:
                    logger.warning(f"正则别名编译失败: {pattern}, 错误: {e}")
            else:
                alias_map[raw] = std_name

    return alias_map, regex_aliases


def parse_allow_list(text):
    return {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}


def parse_blacklist(text):
    exact = set()
    fuzzy = []
    url_black = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("url*"):
            url_black.append(line[4:].strip())
        elif line.startswith("*"):
            fuzzy.append(line[1:].strip())
        else:
            exact.add(line)
    return exact, fuzzy, url_black


def parse_template(text):
    template = OrderedDict()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        std_name = parts[0]
        display_name = parts[1] if len(parts) > 1 else std_name
        group = parts[2] if len(parts) > 2 else ""
        template[std_name] = (display_name, group)
    return template


def parse_network_sources(text):
    sources = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line and not line.startswith("http"):
            group, url = line.split(":", 1)
            sources.append((url.strip(), group.strip()))
        else:
            sources.append((line, None))
    return sources


# ================== 本地源扫描 ==================
def get_local_sources(local_dir, group_mapping):
    sources = []
    local_path = Path(local_dir)
    if not local_path.exists():
        return sources

    for file_path in local_path.rglob("*"):
        if not file_path.is_file():
            continue
        suffix = file_path.suffix.lower()
        if suffix not in [".m3u", ".m3u8", ".txt"]:
            continue

        group = None
        # 1. 子目录匹配
        for part in file_path.relative_to(local_path).parts[:-1]:
            if part in group_mapping.values():
                group = part
                break

        # 2. 文件名前缀匹配
        if not group:
            lower_name = file_path.stem.lower()
            for prefix, mapped_group in group_mapping.items():
                if lower_name.startswith(prefix.lower()):
                    group = mapped_group
                    break

        sources.append((str(file_path), group))
    return sources


# ================== 别名与黑名单 ==================
def apply_aliases(name, alias_map, regex_aliases):
    n = name.strip()

    # 1. 精确匹配
    if n in alias_map:
        return alias_map[n]

    n_key = normalize_name(n)

    # 2. 普通别名匹配
    for raw, std in alias_map.items():
        if n_key == normalize_name(raw):
            return std

    # 3. 正则别名匹配
    for std_name, pattern in regex_aliases:
        if pattern.search(n):
            return std_name

    return name


def is_blacklisted(name, url, exact, fuzzy, url_black):
    if name in exact:
        return True
    for kw in fuzzy:
        if kw and kw in name:
            return True
    for kw in url_black:
        if kw and kw in url:
            return True
    return False


# ================== 测速 ==================

# ================== 测速 ==================

def quick_test_url(session, url, timeout=3):
    """第一阶段：连通性检测，不要求 Range 支持"""
    try:
        import ssl
        import urllib.request
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0"
        })
        req.method = "HEAD"
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            if resp.status < 400:
                return True
    except Exception:
        # HEAD 失败，尝试 GET 前 1KB
        try:
            req2 = urllib.request.Request(url, headers={
                "Range": "bytes=0-1023",
                "User-Agent": "Mozilla/5.0"
            })
            with urllib.request.urlopen(req2, timeout=timeout, context=ctx) as resp:
                if resp.status < 400:
                    return True
        except Exception:
            pass
    return False

def precise_test_url(session, url, timeout=8):
    """第二阶段：尝试获取少量数据，验证流可用"""
    start = time.perf_counter()
    try:
        import ssl
        import urllib.request
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0"
        })
        req.method = "HEAD"
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            latency = time.perf_counter() - start
            if resp.status < 400:
                return True, latency
    except Exception:
        # HEAD 失败，尝试 GET
        try:
            req2 = urllib.request.Request(url, headers={
                "Range": "bytes=0-524287",
                "User-Agent": "Mozilla/5.0"
            })
            with urllib.request.urlopen(req2, timeout=timeout, context=ctx) as resp:
                latency = time.perf_counter() - start
                if resp.status < 400:
                    return True, latency
        except Exception:
            pass
    return False, float("inf")

def ffprobe_check(url, timeout=10):
    """ffprobe 视频流检测，返回 (是否含视频流, 耗时)"""
    if not shutil.which("ffprobe"):
        logger.warning("未找到 ffprobe，跳过视频流检测")
        return False, float("inf")
    try:
        start = time.perf_counter()
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v",
            "-show_entries", "stream=codec_type",
            "-of", "default=noprint_wrappers=1:nokey=1",
            "-rw_timeout", str(int(timeout * 1000000)),
            url
        ]
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout
        )
        latency = time.perf_counter() - start
        output = result.stdout.decode("utf-8", errors="ignore")
        if "video" in output:
            return True, latency
    except subprocess.TimeoutExpired:
        logger.debug(f"ffprobe 检测超时: {url}")
    except Exception as e:
        logger.debug(f"ffprobe 检测失败: {url}, 错误: {e}")
    return False, float("inf")


def clean_url(url):
    url = re.sub(r"\$.*$", "", url).strip()
    return url


# ================== 主构建流程 ==================
def build():
    logger.info("=" * 50)
    logger.info("HI-C IPTV Playlist 开始构建")
    logger.info("=" * 50)

    sources_cfg = load_json(CONFIG_DIR / "sources.json")
    settings = sources_cfg.get("settings", {})

    max_workers = settings.get("max_workers", 10)
    max_sources = settings.get("max_sources_per_channel", 3)
    max_sources_low = settings.get("max_sources_low_link", 5)
    test_timeout = settings.get("test_timeout", 5)
    use_ffprobe = settings.get("use_ffprobe", False)
    ffprobe_timeout = settings.get("ffprobe_timeout", 10)

    local_dir = BASE_DIR / sources_cfg.get("local_sources_dir", "data/sources")
    network_file = BASE_DIR / sources_cfg.get("network_sources_file", "config/sources.txt")
    group_mapping = sources_cfg.get("group_mapping", {})

    # 是否跳过历史失效源
    skip_dead_sources = sources_cfg.get("skip_dead_sources", True)
    skip_dead_set = load_dead_sources(DEAD_SOURCES_LOG) if skip_dead_sources else set()
    if skip_dead_set:
        logger.info(f"发现 {len(skip_dead_set)} 个已知失效源，本次构建将跳过")

    alias_map, regex_aliases = parse_alias(load_text(CONFIG_DIR / "alias.txt"))
    allow_set = parse_allow_list(load_text(CONFIG_DIR / "allow_list.txt"))
    exact_black, fuzzy_black, url_black = parse_blacklist(load_text(CONFIG_DIR / "blacklist.txt"))
    low_link_set = parse_allow_list(load_text(CONFIG_DIR / "low_link_channel.txt"))
    template = parse_template(load_text(CONFIG_DIR / "template_output.txt"))

    # 模板分组映射：标准名 -> 分组
    template_group_map = {std: group for std, (_, group) in template.items() if group}

    # 加载城市关键词，默认 + 用户扩展
    city_keywords = dict(DEFAULT_CITY_KEYWORDS)
    user_city_keywords = sources_cfg.get("city_keywords", {})
    if user_city_keywords:
        for city, kws in user_city_keywords.items():
            if city in city_keywords:
                city_keywords[city] = list(set(city_keywords[city] + kws))
            else:
                city_keywords[city] = kws

    # 加载分组关键词
    full_group_keywords = dict(DEFAULT_GROUP_KEYWORDS)
    user_group_keywords = sources_cfg.get("group_keywords", {})
    if user_group_keywords:
        for group, kws in user_group_keywords.items():
            if group in full_group_keywords:
                full_group_keywords[group] = list(set(full_group_keywords[group] + kws))
            else:
                full_group_keywords[group] = kws

    # {group: {name: [(url, source_id)]}}
    raw_channels = defaultdict(lambda: defaultdict(list))
    source_channels = defaultdict(set)  # source_id -> set of std_names
    channel_sources = defaultdict(set)  # std_name -> set of source_ids

    # 1. 本地源
    local_sources = get_local_sources(local_dir, group_mapping)
    logger.info(f"发现本地源文件: {len(local_sources)} 个")
    for file_path, group in local_sources:
        if file_path in skip_dead_set:
            logger.info(f"  跳过已知失效本地源: {file_path}")
            continue
        logger.info(f"  读取本地源: {file_path}, 分组: {group or '自动'}")
        content = load_text(Path(file_path))
        if not content:
            log_dead_source("本地源", file_path, "文件内容为空或读取失败")
            continue
        file_channel_count = 0
        for name, ch_url, _ in parse_source(content):
            if is_blacklisted(name, ch_url, exact_black, fuzzy_black, url_black):
                continue
            std_name = apply_aliases(name, alias_map, regex_aliases)
            final_group = template_group_map.get(std_name)
            if not final_group:
                final_group = group or classify_channel(std_name, full_group_keywords, city_keywords)
            raw_channels[final_group][std_name].append((ch_url, file_path))
            source_channels[file_path].add(std_name)
            channel_sources[std_name].add(file_path)
            file_channel_count += 1
        if file_channel_count == 0:
            log_dead_source("本地源", file_path, "未解析到有效频道")

    # 2. 网络源
    network_sources = parse_network_sources(load_text(network_file))
    logger.info(f"网络源数量: {len(network_sources)} 个")
    for url, group in network_sources:
        if url in skip_dead_set:
            logger.info(f"  跳过已知失效网络源: {url}")
            continue
        content = download_text(url)
        if not content:
            log_dead_source("网络源", url, "下载失败或返回空内容")
            continue
        url_channel_count = 0
        for name, ch_url, _ in parse_source(content):
            if is_blacklisted(name, ch_url, exact_black, fuzzy_black, url_black):
                continue
            std_name = apply_aliases(name, alias_map, regex_aliases)
            final_group = template_group_map.get(std_name)
            if not final_group:
                final_group = group or classify_channel(std_name, full_group_keywords, city_keywords)
            raw_channels[final_group][std_name].append((ch_url, url))
            source_channels[url].add(std_name)
            channel_sources[std_name].add(url)
            url_channel_count += 1
        if url_channel_count == 0:
            log_dead_source("网络源", url, "未解析到有效频道")

    # 3. 去重
    logger.info("对 URL 去重...")
    for group in list(raw_channels.keys()):
        for ch_name in list(raw_channels[group].keys()):
            seen = set()
            unique = []
            for url, source_id in raw_channels[group][ch_name]:
                clean = clean_url(url)
                if clean not in seen and clean.startswith("http"):
                    seen.add(clean)
                    unique.append((clean, source_id))
            raw_channels[group][ch_name] = unique

    # 4. 白名单过滤
    if allow_set:
        logger.info("应用白名单过滤...")
        for group in list(raw_channels.keys()):
            for ch_name in list(raw_channels[group].keys()):
                if ch_name not in allow_set:
                    del raw_channels[group][ch_name]

    # 5. 测速或跳过
    skip_speed_test = settings.get("skip_speed_test", False)
    tested = defaultdict(lambda: defaultdict(list))

    if skip_speed_test:
        logger.info("跳过测速，直接使用所有去重后的 URL")
        for group in raw_channels:
            for ch_name, items in raw_channels[group].items():
                for url, source_id in items:
                    tested[group][ch_name].append((url, 0.0))
    else:
        logger.info("开始两阶段测速...")
        all_urls = []
        for group in raw_channels:
            for ch_name, items in raw_channels[group].items():
                for url, source_id in items:
                    all_urls.append((group, ch_name, url))

        session = get_session()

        # 第一阶段：快速筛选
        logger.info(f"第一阶段快速筛选 {len(all_urls)} 个 URL...")
        quick_pass_urls = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {}
            for group, ch_name, url in all_urls:
                f = pool.submit(quick_test_url, session, url)
                future_map[f] = (group, ch_name, url)

            for future in as_completed(future_map):
                group, ch_name, url = future_map[future]
                if future.result():
                    quick_pass_urls.append((group, ch_name, url))

        logger.info(f"第一阶段通过 {len(quick_pass_urls)}/{len(all_urls)} 个 URL")

        # 第二阶段：精确测速
        logger.info("第二阶段精确测速...")
        precise_pass_urls = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {}
            for group, ch_name, url in quick_pass_urls:
                f = pool.submit(precise_test_url, session, url, test_timeout)
                future_map[f] = (group, ch_name, url)

            for future in as_completed(future_map):
                group, ch_name, url = future_map[future]
                ok, latency = future.result()
                if ok:
                    precise_pass_urls.append((group, ch_name, url, latency))

        # 第三阶段：ffprobe 视频流检测（可选）
        if use_ffprobe:
            if shutil.which("ffprobe"):
                logger.info(f"第三阶段 ffprobe 视频流检测 {len(precise_pass_urls)} 个 URL...")
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    future_map = {}
                    for group, ch_name, url, latency in precise_pass_urls:
                        f = pool.submit(ffprobe_check, url, ffprobe_timeout)
                        future_map[f] = (group, ch_name, url, latency)

                    for future in as_completed(future_map):
                        group, ch_name, url, latency = future_map[future]
                        ok, _ = future.result()
                        if ok:
                            tested[group][ch_name].append((url, latency))
            else:
                logger.warning("配置启用 ffprobe 但未找到 ffprobe，仅使用 HTTP 测速结果")
                for group, ch_name, url, latency in precise_pass_urls:
                    tested[group][ch_name].append((url, latency))
        else:
            for group, ch_name, url, latency in precise_pass_urls:
                tested[group][ch_name].append((url, latency))

    # 6. 生成 playlist.m3u
    group_order = [
        "央视", "卫视", "城市", "港澳", "台",
        "日本", "韩国", "美国", "英国", "欧洲", "国际"
    ]

    lines = ['#EXTM3U x-tvg-url="https://epg.112114.xyz/pp.xml"']
    total = 0

    # 使用模板顺序
    template_used = set()
    for std_name, (display_name, template_group) in template.items():
        group = template_group if template_group else classify_channel(std_name, full_group_keywords, city_keywords)
        if group not in tested or std_name not in tested[group]:
            continue
        urls = tested[group][std_name]
        if not urls:
            continue
        urls.sort(key=lambda x: x[1])
        keep_count = max_sources_low if std_name in low_link_set else max_sources
        keep = urls[:keep_count]
        total += 1
        template_used.add(std_name)
        for url, latency in keep:
            lines.append(f'#EXTINF:-1 group-title="{group}",{display_name}')
            lines.append(url)

    # 模板之外的频道
    for group in group_order:
        if group not in tested:
            continue
        for ch_name in sorted(tested[group].keys()):
            if ch_name in template_used:
                continue
            urls = tested[group][ch_name]
            if not urls:
                continue
            urls.sort(key=lambda x: x[1])
            keep_count = max_sources_low if ch_name in low_link_set else max_sources
            keep = urls[:keep_count]
            total += 1
            for url, latency in keep:
                lines.append(f'#EXTINF:-1 group-title="{group}",{ch_name}')
                lines.append(url)

    with open(OUTPUT_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    # 统计
    logger.info(f"构建完成，总有效频道数: {total}")
    for group in group_order:
        count = len(tested.get(group, {}))
        if count:
            logger.info(f"  {group}: {count} 个频道")

    # 源统计报告
    logger.info("生成源统计报告...")
    source_stats = {}
    for source_id, ch_names in source_channels.items():
        source_stats[source_id] = {"total": len(ch_names), "good": 0, "failed": 0}

    good_channels = set()
    for group in tested:
        for ch_name in tested[group]:
            good_channels.add(ch_name)

    for ch_name in good_channels:
        if ch_name in channel_sources:
            for source_id in channel_sources[ch_name]:
                if source_id in source_stats:
                    source_stats[source_id]["good"] += 1

    for source_id, stats in source_stats.items():
        stats["failed"] = stats["total"] - stats["good"]

    # 写入日志文件
    report_lines = [
        "# 源统计报告",
        f"# 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "# 格式: 源 | 总频道数 | 有效频道数 | 失效频道数",
        ""
    ]
    sorted_sources = sorted(source_stats.items(), key=lambda x: (-x[1]["good"], -x[1]['total']))
    for source_id, stats in sorted_sources:
        report_lines.append(f"{source_id} | {stats['total']} | {stats['good']} | {stats['failed']}")

    report_path = LOG_DIR / "sources_report.log"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(report_lines) + "\n")
        logger.info(f"源统计报告已保存: {report_path}")
    except Exception as e:
        logger.warning(f"保存源统计报告失败: {e}")

    # 控制台输出前 10
    if sorted_sources:
        logger.info("源统计 TOP10（按有效频道数排序）:")
        for source_id, stats in sorted_sources[:10]:
            logger.info(f"  {source_id}: total={stats['total']}, good={stats['good']}, failed={stats['failed']}")


if __name__ == "__main__":
    build()