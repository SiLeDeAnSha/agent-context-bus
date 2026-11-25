"""Persistent project aliases and reviewable suggestions for daily progress."""

from __future__ import annotations

from collections import defaultdict
import json
import os
import unicodedata
from uuid import NAMESPACE_URL, uuid5

from multi_machine import atomic_write_json, file_lock


REGISTRY_FILE = "project_registry.json"
UNNAMED = "未命名项目"


def name_key(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def project_id(name: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"daily-summary/project/{name_key(name)}"))


def item_name(item: dict) -> str:
    return str(item.get("project") or "").strip() or UNNAMED


class ProjectCatalog:
    def __init__(self, days: list[dict], registry: dict | None = None):
        self.projects: dict[str, dict] = {}
        self.by_name: dict[str, str] = {}
        self.by_id: dict[str, str] = {}
        for project in (registry or {}).get("projects", []):
            self._add(project["project"], project["aliases"])
        self.confirmed_names = set(self.by_name)
        for day in reversed(days):
            for item in day.get("work_items") or []:
                name = item_name(item)
                if name_key(name) not in self.by_name:
                    self._add(name, [])

    def _add(self, name: str, aliases: list[str]) -> None:
        key = project_id(name)
        self.projects[key] = {"project_id": key, "project": name, "aliases": aliases}
        for label in [name, *aliases]:
            self.by_name[name_key(label)] = key
            self.by_id[project_id(label)] = key

    def lookup(self, label: str) -> dict | None:
        key = name_key(label)
        return self.projects.get(self.by_name.get(key) or self.by_id.get(key))

    def for_item(self, item: dict) -> dict:
        name = item_name(item)
        return self.lookup(name) or {"project_id": project_id(name), "project": name, "aliases": []}

    def briefs(self, raw_briefs: dict) -> dict:
        grouped: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for label, brief in raw_briefs.items():
            if not isinstance(brief, dict):
                continue
            name = self.for_item({"project": label})["project"]
            for tool, raw_value in brief.items():
                value = str(raw_value or "").strip()
                values = grouped[name][tool]
                if value and value != "-" and value not in values:
                    values.append(value)
        return {name: {tool: "；".join(values) for tool, values in tools.items() if values}
                for name, tools in grouped.items()}

    def suggestions(self, days: list[dict]) -> list[dict]:
        grouped: dict[tuple[str, str], dict] = {}
        for day in reversed(days):
            for item in day.get("work_items") or []:
                name = item_name(item)
                suggested_id = item.get("suggested_project_id")
                if (
                    name_key(name) in self.confirmed_names
                    or not isinstance(suggested_id, str)
                    or suggested_id not in self.by_id
                ):
                    continue
                current = self.for_item(item)
                candidate = self.projects[self.by_id[suggested_id]]
                if current["project_id"] == candidate["project_id"]:
                    continue
                key = (name_key(name), candidate["project_id"])
                suggestion = grouped.setdefault(
                    key,
                    {"label": name, "current": current, "suggested": candidate,
                     "reason": item.get("project_suggestion_reason", ""), "dates": []},
                )
                if day["workday"] not in suggestion["dates"]:
                    suggestion["dates"].append(day["workday"])
        return list(grouped.values())


def load_registry(data_dir: str) -> dict:
    try:
        with open(os.path.join(data_dir, REGISTRY_FILE), encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"projects": []}


def edit_registry(data_dir: str, days: list[dict], action: str, target: str, labels: list[str]) -> dict:
    """Save an explicit merge or split. Preserve original labels in daily history."""
    if not isinstance(target, str) or not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise ValueError("请提供项目名称或 ID。")
    target = target.strip()
    labels = list(dict.fromkeys(label.strip() for label in labels))
    if not target or any(not label for label in labels):
        raise ValueError("项目名称和别名不能为空。")
    with file_lock(os.path.join(data_dir, ".project_registry.lock")):
        registry = load_registry(data_dir)
        catalog = ProjectCatalog(days, registry)
        projects = {project["project"]: list(project["aliases"]) for project in registry["projects"]}
        known = catalog.lookup(target)
        if action == "merge":
            if not labels:
                raise ValueError("请提供至少一个要合并的项目或别名。")
            target = known["project"] if known else target
            aliases = projects.setdefault(target, [])
            seen = {name_key(label) for label in [target, *aliases]}
            for label in labels:
                source = catalog.lookup(label)
                if source and source["project"] == target:
                    continue
                moved = [source["project"], *source["aliases"]] if source else [label]
                if source:
                    projects.pop(source["project"], None)
                for name in moved:
                    key = name_key(name)
                    if key not in seen:
                        aliases.append(name)
                        seen.add(key)
        elif action == "split":
            # An alias can become a project again under its original ID.
            if known and name_key(target) == known["project_id"]:
                raise ValueError("拆分请使用原始别名或新的项目名称。")
            if known and name_key(known["project"]) == name_key(target):
                target = known["project"]
            if not known and not labels:
                raise ValueError("请提供要拆出的别名。")
            moving = {name_key(label) for label in [target, *labels]}
            for project in catalog.projects.values():
                name = project["project"]
                if name != target and name_key(name) in moving:
                    raise ValueError(f"「{name}」已有独立身份。请用合并移动整个项目。")
            for name, aliases in projects.items():
                if name != target:
                    projects[name] = [alias for alias in aliases if name_key(alias) not in moving]
            aliases = {}
            for label in [*projects.get(target, []), *labels]:
                if name_key(label) != name_key(target):
                    aliases.setdefault(name_key(label), label)
            projects[target] = list(aliases.values())
        else:
            raise ValueError("操作必须是 merge 或 split。")

        updated = {"projects": [{"project_id": project_id(name), "project": name, "aliases": aliases}
                                for name, aliases in projects.items()]}
        atomic_write_json(os.path.join(data_dir, REGISTRY_FILE), updated)
        return ProjectCatalog(days, updated).lookup(target)
