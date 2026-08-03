"""Generate the checked-in example artifacts with the production renderer."""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wechat_decrypt_tool.modules.wechat_emoji import emojify_wechat_shortcodes
from wxmoments import ExportedPost, format_post_heading, render_pdf, write_pdf_html

OUTPUT_DIR = Path(__file__).resolve().parent
FIGURE_DIR = OUTPUT_DIR / "figure"
AI_PICS = Path(r"E:\OneDrive\Desktop\AIPictures")

IMAGE_MAP = {
    "hotpot": "elegant_chinese_hot_pot_menu_design_HD_2x_fast.png",
    "wechat_tf": "wechat_transfer_xidele.png",
    "weibo": "weibo_hot_search_trends_overview_HD_2x_fast.png",
    "infographic": "standard_model_infographic_in_neon_style_HD_2x_fast.png",
    "dorm_pc": "dorm_working_on_pc.png",
    "nailong_l": "laughing_nailong.png",
    "nailong_d": "nailong's_dinner.jpg",
}

PEOPLE = {
    "ma": {"name": "老妈", "wxid": "wxid_ma", "remark": "妈妈", "region": "广东广州", "sig": "养花养草养孩子"},
    "ba": {"name": "老爸", "wxid": "wxid_ba", "remark": "老爸", "region": "广东广州", "sig": "退休生活美滋滋"},
    "zhang": {"name": "张老师", "wxid": "wxid_zhang", "remark": "张老师(数学)", "region": "北京海淀", "sig": "数学之美"},
    "li": {"name": "李教授", "wxid": "wxid_li", "remark": "李教授(物理)", "region": "湖北武汉", "sig": "物理改变世界"},
    "jie": {"name": "阿杰", "wxid": "wxid_jie", "remark": "阿杰(死党)", "region": "广东广州", "sig": "吃饭不积极思想有问题"},
    "wang": {"name": "小王", "wxid": "wxid_wang", "remark": "小王(同事)", "region": "广东深圳", "sig": "996间歇性emo"},
    "biao": {"name": "表姐", "wxid": "wxid_biao", "remark": "表姐(上海)", "region": "上海浦东", "sig": "不想上班想开店"},
    "mei": {"name": "表妹", "wxid": "wxid_mei", "remark": "表妹(大学生)", "region": "湖北武汉", "sig": "期末周毁灭吧"},
    "liu": {"name": "大刘", "wxid": "wxid_liu", "remark": "大刘(铁瓷)", "region": "广东广州", "sig": "健身撸铁日常"},
    "shan": {"name": "珊珊", "wxid": "wxid_shan", "remark": "珊珊", "region": "广东广州", "sig": "猫狗双全人生赢家"},
    "chen": {"name": "陈哥", "wxid": "wxid_chen", "remark": "陈哥(前同事)", "region": "浙江杭州", "sig": "创业中"},
    "lao": {"name": "隔壁老王", "wxid": "wxid_lao", "remark": "邻居老王", "region": "广东广州", "sig": "象棋钓鱼带孙子"},
    "da": {"name": "大师兄", "wxid": "wxid_da", "remark": "大师兄(实验室)", "region": "北京海淀", "sig": "论文论文论文"},
    "xiao": {"name": "小李", "wxid": "wxid_xiao", "remark": "小李(新同事)", "region": "广东深圳", "sig": "实习生努力中"},
    "fang": {"name": "芳姐", "wxid": "wxid_fang", "remark": "芳姐(领导)", "region": "广东广州", "sig": "女强人也是宝妈"},
    "tao": {"name": "阿涛", "wxid": "wxid_tao", "remark": "阿涛(高中同学)", "region": "广东广州", "sig": "程序员但头发还在"},
    "jing": {"name": "静静", "wxid": "wxid_jing", "remark": "静静(室友)", "region": "上海", "sig": "考研二战中"},
    "ayi": {"name": "房东阿姨", "wxid": "wxid_ayi", "remark": "房东阿姨", "region": "广东广州", "sig": "收租是主业养猫是副业"},
    "jiao": {"name": "教练", "wxid": "wxid_jiao", "remark": "私教阿Ken", "region": "广东广州", "sig": "不练出腹肌不换头像"},
    "er": {"name": "二叔", "wxid": "wxid_er", "remark": "二叔", "region": "广东潮州", "sig": "喝茶看报天下事"},
}

POSTS = [
    {
        "time": "2026-07-28 12:30:15",
        "user": "老妈",
        "loc": "",
        "body": "今天在市场买到了很好的土猪肉，给家里人炖了一锅莲藕排骨汤[愉快]\n顺便卤了一锅牛腱子，明天早餐夹馒头吃🥰",
        "imgs": ["hotpot", "nailong_d"],
        "likes": ["zhang", "biao", "shan"],
        "comments": [
            ("zhang", "看着就好吃！改天教教我怎么做[色]"),
            ("biao", "舅妈手艺一绝！周末我来蹭饭🥺"),
            ("shan", "阿姨我也想喝[可怜]"),
        ],
    },
    {
        "time": "2026-07-24 22:35:18",
        "user": "阿杰",
        "loc": "广州·猎德",
        "body": "今晚跟兄弟去猎德吃大排档，炒牛河配冰镇啤酒，舒服到翻白眼😋\n隔壁桌大哥聊几百万项目，我俩默默算人均38怎么再省4块[捂脸]",
        "imgs": ["nailong_l", "wechat_tf", "weibo"],
        "likes": ["wang", "liu", "jiao"],
        "comments": [
            ("wang", "人均38还凑满减，不愧是你[旺柴]"),
            ("liu", "下次带上我啊！我请客你买单"),
            ("jiao", "炒牛河碳水爆炸，明天来加练💪"),
        ],
    },
    {
        "time": "2026-07-22 20:10:44",
        "user": "表妹",
        "loc": "学校·图书馆",
        "body": "期末周在图书馆待到闭馆出来😇\n发现下雨没带伞，正要冲出去，一个小姐姐主动说「我们一起走吧，我伞大」🥺\n世界上还是好人多啊！顺便说大物的 Chladni 图真的好难画😭",
        "imgs": ["dorm_pc", "infographic"],
        "likes": ["biao", "jie", "shan"],
        "comments": [
            ("biao", "表妹加油！考完请你吃火锅🍲"),
            ("jie", "小姐姐是不是看上你了才撑伞的🤔"),
            ("mei", "@阿杰 别瞎说！人家就是好心🥲", "jie"),
        ],
    },
]


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def folder_name(value: str) -> str:
    return parse_time(value).strftime("%Y%m%d_%H%M%S")


def source_image(key: str) -> Path:
    return AI_PICS / IMAGE_MAP[key]


def copy_images() -> dict[tuple[str, str], str]:
    copied: dict[tuple[str, str], str] = {}
    for post in POSTS:
        folder = folder_name(post["time"])
        target_dir = FIGURE_DIR / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        for index, key in enumerate(post["imgs"], 1):
            src = source_image(key)
            suffix = src.suffix.lower()
            target = target_dir / f"{index:02d}{suffix}"
            if src.exists():
                shutil.copy2(src, target)
            copied[(post["time"], key)] = target.relative_to(OUTPUT_DIR).as_posix()
    return copied


def interaction_markdown(post: dict[str, object]) -> str:
    lines: list[str] = []
    likes = post.get("likes") or []
    if likes:
        lines.append(f"**❤️ 点赞**：{'、'.join(PEOPLE[key]['name'] for key in likes)}")
    comments = post.get("comments") or []
    if comments:
        if lines:
            lines.append("")
        lines.append("**💬 评论**")
        for comment in comments:
            uid, text = comment[0], emojify_wechat_shortcodes(comment[1])
            reply_to = comment[2] if len(comment) > 2 else ""
            if reply_to:
                lines.append(f"- {PEOPLE[uid]['name']} 回复 {PEOPLE[reply_to]['name']}：{text}")
            else:
                lines.append(f"- {PEOPLE[uid]['name']}：{text}")
    return "\n".join(lines).strip()


def exported_posts(image_rels: dict[tuple[str, str], str]) -> list[ExportedPost]:
    posts: list[ExportedPost] = []
    for post in POSTS:
        body = emojify_wechat_shortcodes(str(post["body"]))
        images = [image_rels[(post["time"], key)] for key in post["imgs"]]
        posts.append(
            ExportedPost(
                time_text=str(post["time"]),
                display=str(post["user"]),
                location=str(post.get("loc") or ""),
                body=body,
                images=images,
                interactions=interaction_markdown(post),
            )
        )
    return posts


def write_markdown(posts: list[ExportedPost]) -> None:
    lines = ["# 微信朋友圈备份", ""]
    for post in posts:
        lines.extend([f"## {format_post_heading(post.time_text, post.display)}", "", post.body, ""])
        for index, rel in enumerate(post.images, 1):
            lines.extend([f"![{post.time_text} image {index}]({rel})", ""])
        if post.interactions:
            lines.extend([post.interactions, ""])
        lines.extend(["---", ""])
    (OUTPUT_DIR / "moments.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_contacts() -> None:
    rows = sorted(
        [
            {
                "wxid": value["wxid"],
                "昵称": value["name"],
                "备注名": value["remark"],
                "其他信息": f"地区: {value['region']}；签名: {value['sig']}",
            }
            for value in PEOPLE.values()
        ],
        key=lambda row: row["备注名"],
    )
    (OUTPUT_DIR / "contacts.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with (OUTPUT_DIR / "contacts.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["wxid", "昵称", "备注名", "其他信息"])
        writer.writeheader()
        writer.writerows(rows)


def render_previews() -> None:
    try:
        import fitz
    except Exception as exc:
        print(f"  ⚠️ 预览图生成跳过: {exc}")
        return

    pdf_path = OUTPUT_DIR / "moments.pdf"
    doc = fitz.open(str(pdf_path))
    print(f"渲染预览图（PDF 共 {len(doc)} 页）")
    for page_num in range(len(doc)):
        page = doc[page_num]
        pix = page.get_pixmap(matrix=fitz.Matrix(150 / 72, 150 / 72))
        out = OUTPUT_DIR / f"preview-{page_num + 1}.png"
        pix.save(str(out))
        print(f"  ✅ {out.name} ({pix.width}x{pix.height})")
    doc.close()


def force_remove(path: Path) -> None:
    def onexc(function, target, exc_info) -> None:
        os.chmod(target, 0o700)
        function(target)

    if path.is_dir():
        shutil.rmtree(path, onexc=onexc)
    else:
        path.unlink(missing_ok=True)


def main() -> None:
    if FIGURE_DIR.exists():
        force_remove(FIGURE_DIR)
    for pattern in ("moments.*", "contacts.*", "params.json", "preview-*.png"):
        for path in OUTPUT_DIR.glob(pattern):
            force_remove(path)
    FIGURE_DIR.mkdir(parents=True, exist_ok=True)

    params = {
        "start_time": "20260720",
        "end_time": "",
        "only_self": False,
        "keep_interactions": True,
        "export_contacts": True,
        "friend_inputs": [],
    }
    (OUTPUT_DIR / "params.json").write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")

    image_rels = copy_images()
    posts = exported_posts(image_rels)
    write_markdown(posts)
    write_pdf_html(OUTPUT_DIR, posts)
    render_pdf(OUTPUT_DIR, posts, OUTPUT_DIR / "moments.pdf")
    write_contacts()
    render_previews()
    print(f"✅ {len(posts)} 条朋友圈 | {len(image_rels)} 张图片 | example 已更新")


if __name__ == "__main__":
    main()
