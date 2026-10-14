import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import zipfile

from . import __version__
from .android import apk_info, archive_diff
from .build import boolean, build_images, package, plan
from .compare import compare_trees, three_way_trees, three_way_images
from .evidence import parse_log
from .extract import extract
from .inventory import inventory, three_way
from .io import read_json, write_json
from .recipes import apply_recipe
from .repack import repack_erofs
from .reconstruct import derive, stage, rebuild, verify, import_assets, verify_assets
from .properties import port_properties
from .pipeline import execute_sample
from .ota import payload_info
from .tools import ROOT, executable, install_tool


def parser():
    p = argparse.ArgumentParser(description="ColorNT Oplus ROM analysis and verified build recipes")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    data_pack = sub.add_parser('import-v14-pack', help='Verify and install the portable v1.4 mod data ZIP')
    data_pack.add_argument('pack', type=Path)
    data_export = sub.add_parser('export-v14-pack', help='Package pinned mod differences; no A/B ROM images')
    data_export.add_argument('--out', required=True, type=Path)
    sub.add_parser('setup-policy', help='Prepare the isolated portable policy compiler (requires WSL2 on Windows)')
    port = sub.add_parser('port', help='Build B OS on A hardware with the same pipeline as the GUI (mods OFF by default)')
    port.add_argument('--base', required=True, type=Path)
    port.add_argument('--donor', required=True, type=Path)
    port.add_argument('--output-parent', required=True, type=Path, help='Create a new candidate folder inside this location')
    port.add_argument('--plan', action='store_true', help='Metadata/header preview only; no build or device commands')
    port.add_argument('--v14-full', action='store_true', help='Reconstruct the pinned v1.4 OOS full mod/toolbox variant; exact A/B versions only')
    port.add_argument('--source-metadata', action='store_true', help='Keep source metadata instead of the default unlocked lab policy; package may be blocked')
    port.add_argument('--apk', action='append', default=[], type=Path)
    port.add_argument('--img', action='append', default=[], metavar='PARTITION=PATH')
    port.add_argument('--boot-animation', type=Path)
    port.add_argument('--reboot-animation', type=Path)
    port.add_argument('--report', type=Path)
    inspect = sub.add_parser("inspect", help="Inventory a ROM folder, ZIP or image")
    inspect.add_argument("source", type=Path)
    inspect.add_argument("--no-hash", action="store_true")
    inspect.add_argument("--workers", type=int, default=2)
    inspect.add_argument("--report", type=Path, required=True)
    ext = sub.add_parser("extract", help="Extract payload/EROFS to a new workspace")
    ext.add_argument("source", type=Path)
    ext.add_argument("--out", type=Path, required=True)
    ext.add_argument("--partitions", nargs="+")
    ext.add_argument("--no-hash", action="store_true")
    ext.add_argument("--report", type=Path)
    log = sub.add_parser("analyze-log")
    log.add_argument("source", type=Path)
    log.add_argument("--report", type=Path, required=True)
    payload = sub.add_parser("payload-info", help="Inspect bounded OTA manifest and dynamic group capacities")
    payload.add_argument("source", type=Path)
    payload.add_argument("--report", type=Path, required=True)
    compare = sub.add_parser("compare", help="Three SHA-256 inventory manifests")
    for name in ("base", "donor", "output"):
        compare.add_argument("--" + name, type=Path, required=True)
    compare.add_argument("--report", type=Path, required=True)
    diff = sub.add_parser("diff", help="Byte-compare two extracted filesystems")
    diff.add_argument("left", type=Path)
    diff.add_argument("right", type=Path)
    diff.add_argument("--report", type=Path, required=True)
    apk = sub.add_parser("apk-info")
    apk.add_argument("source", type=Path)
    apk.add_argument("--report", type=Path)
    adiff = sub.add_parser("archive-diff", help="Compare APK/JAR internal members")
    adiff.add_argument("left", type=Path)
    adiff.add_argument("right", type=Path)
    adiff.add_argument("--report", type=Path, required=True)
    for command in ("compare-trees", "compare-images"):
        triple = sub.add_parser(command, help="Three-way byte comparison without ROM hashes")
        for name in ("base", "donor", "output"):
            triple.add_argument("--" + name, type=Path, required=True)
        triple.add_argument("--report", type=Path, required=True)
    recover = sub.add_parser("derive-reconstruction", help="Recover a fixed-sample source map and mod assets")
    for name in ("base", "donor", "reference", "file-report", "image-report", "extraction-report", "assets", "recipe"):
        recover.add_argument("--" + name, type=Path, required=True)
    recover.add_argument("--id", required=True)
    recover.add_argument("--capacity-report", type=Path)
    recover.add_argument("--group-assignment", action="append", default=[], metavar="PARTITION=GROUP")
    recover.add_argument("--report", type=Path, required=True)
    restore = sub.add_parser("import-reconstruction-assets", help="Recover ignored mod assets in a fresh checkout")
    for name in ("recipe", "reference", "reference-images", "assets", "report"):
        restore.add_argument("--" + name, type=Path, required=True)
    assets_check = sub.add_parser("verify-reconstruction-assets", help="Check the complete local asset store against its recipe")
    for name in ("recipe", "assets", "report"):
        assets_check.add_argument("--" + name, type=Path, required=True)
    props = sub.add_parser("port-properties", help="Apply evidence-scoped A/B property transformations to a disposable staged tree")
    props.add_argument("stage", type=Path)
    for name in ("base", "donor", "policy", "report"):
        props.add_argument("--" + name, type=Path, required=True)
    props.add_argument("--dry-run", action="store_true")
    stg = sub.add_parser("stage-reconstruction", help="Compose port files from A, B and recovered assets")
    for name in ("base", "donor", "assets", "recipe", "out", "report"):
        stg.add_argument("--" + name, type=Path, required=True)
    reb = sub.add_parser("rebuild-reconstruction", help="Repack and assemble recovered sample candidate")
    for name in ("base", "donor", "assets", "recipe", "stage", "out", "report"):
        reb.add_argument("--" + name, type=Path, required=True)
    reb.add_argument("--partitions", nargs="+")
    reb.add_argument("--resume", action="store_true", help="Resume only an owned candidate with its build report")
    ver = sub.add_parser("verify-reconstruction", help="Validate reconstructed content and Android metadata")
    for name in ("stage", "unpacked", "recipe", "report"):
        ver.add_argument("--" + name, type=Path, required=True)
    ver.add_argument("--partitions", nargs="+")
    bop = sub.add_parser("/bop", aliases=["bop", "plan"], help="B to A build plan; execute only verified profiles")
    bop.add_argument("base")
    bop.add_argument("-port", "--port", dest="donor")
    bop.add_argument("--profile", type=Path, default=ROOT / "profiles" / "pjf110-cph2745-oos16.json")
    bop.add_argument("--mode", choices=["fastboot", "ota", "both"], default=os.getenv("BUILD_MODE", "both"))
    bop.add_argument("--mods", type=boolean, default=boolean(os.getenv("INSTALL_MODS", "true")))
    bop.add_argument("--toolbox", type=boolean, default=boolean(os.getenv("INSTALL_TOOLBOX", "true")))
    bop.add_argument("--report", type=Path)
    bop.add_argument("--execute", action="store_true")
    bop.add_argument("--out", type=Path)
    bop.add_argument("--assets", type=Path)
    bop.add_argument("--base-workspace", type=Path)
    bop.add_argument("--donor-workspace", type=Path)
    recipe = sub.add_parser("apply", help="Apply hash-guarded recipe to a staged tree")
    recipe.add_argument("root", type=Path)
    recipe.add_argument("recipe", type=Path)
    recipe.add_argument("--assets", type=Path, required=True)
    recipe.add_argument("--report", type=Path, required=True)
    repack = sub.add_parser("repack-erofs")
    repack.add_argument("tree", type=Path)
    repack.add_argument("--config", type=Path, required=True)
    repack.add_argument("--out", type=Path, required=True)
    repack.add_argument("--timestamp", type=int, required=True)
    repack.add_argument("--uuid", required=True)
    pack = sub.add_parser("pack")
    pack.add_argument("source", type=Path)
    pack.add_argument("--out", type=Path, required=True)
    tool = sub.add_parser("setup-tool")
    tool.add_argument("id", choices=["erofs-windows", "payload-windows", "erofs-linux", "payload-linux"])
    sub.add_parser("doctor")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        code = 0
        if args.command == 'import-v14-pack':
            from .portable import import_v14_pack
            result = import_v14_pack(args.pack, lambda text: print(text, flush=True))
        elif args.command == 'export-v14-pack':
            from .portable import export_v14_pack
            result = export_v14_pack(args.out)
        elif args.command == 'setup-policy':
            from .policy_runtime import setup
            result = setup(lambda text: print(text, flush=True))
        elif args.command == 'port':
            from .product import selection, preview, create
            replacements = []
            for item in args.img:
                name, sep, path = item.partition('=')
                if not name or not sep or not path:
                    raise ValueError('--img requires PARTITION=PATH')
                replacements.append({'partition': name, 'path': path})
            animations = {k: getattr(args, k) for k in ('boot_animation', 'reboot_animation') if getattr(args, k)}
            selected = selection(args.base, args.donor, args.output_parent, unlocked=not args.source_metadata,
                                 apks=args.apk, replacements=replacements, animations=animations, v14=args.v14_full)
            if args.report:
                target = args.report.resolve()
                if target.exists() or any(target == p.resolve() or target.is_relative_to(p.resolve()) for p in (args.base, args.donor)):
                    raise ValueError('Choose a new report path outside protected A/B inputs')
                from .product import check_output_parent
                check_output_parent(target.parent)
            result = preview(selected) if args.plan else create(selected, lambda e: print(e['message'], flush=True) if e.get('type') == 'progress' else None)
            if result['status'] not in ('planned', 'experimental-package-ready'):
                code = 2
        elif args.command == "inspect":
            if not 1 <= args.workers <= 16:
                raise ValueError("Workers must be between 1 and 16")
            result = inventory(args.source, not args.no_hash, args.workers)
        elif args.command == "extract":
            result = extract(args.source, args.out, args.partitions, not args.no_hash)
        elif args.command == "analyze-log":
            result = parse_log(args.source.read_text(encoding="utf-8-sig"))
        elif args.command == "payload-info":
            result = payload_info(args.source)
        elif args.command == "compare":
            result = three_way(*(read_json(x) for x in (args.base, args.donor, args.output)))
        elif args.command == "diff":
            result = compare_trees(args.left, args.right)
        elif args.command == "apk-info":
            result = apk_info(args.source)
        elif args.command == "archive-diff":
            result = archive_diff(args.left, args.right)
        elif args.command == "compare-trees":
            result = three_way_trees(args.base, args.donor, args.output)
        elif args.command == "compare-images":
            result = three_way_images(args.base, args.donor, args.output)
        elif args.command == "derive-reconstruction":
            assignments = {}
            for item in args.group_assignment:
                parts = item.split("=", 1)
                if len(parts) != 2 or not all(parts) or parts[0] in assignments:
                    raise ValueError("Invalid/duplicate group assignment")
                assignments[parts[0]] = parts[1]
            result = derive(args.base, args.donor, args.reference, args.file_report,
                            args.image_report, args.extraction_report, args.assets, args.recipe, args.id,
                            args.capacity_report, assignments)
        elif args.command == "stage-reconstruction":
            result = stage(args.base, args.donor, args.assets, args.recipe, args.out)
        elif args.command == "import-reconstruction-assets":
            result = import_assets(args.recipe, args.reference, args.reference_images, args.assets)
        elif args.command == "verify-reconstruction-assets":
            result = verify_assets(args.recipe, args.assets)
            if result["status"] != "pass":
                code = 2
        elif args.command == "port-properties":
            result = port_properties(args.stage, args.base, args.donor, read_json(args.policy), args.dry_run)
        elif args.command == "rebuild-reconstruction":
            result = rebuild(args.base, args.donor, args.assets, args.recipe, args.stage,
                             args.out, args.partitions, args.resume)
        elif args.command == "verify-reconstruction":
            result = verify(args.stage, args.unpacked, args.recipe, args.partitions)
            if result["status"] != "pass":
                code = 2
        elif args.command in ("/bop", "bop", "plan"):
            result = plan(args.base, args.donor, args.profile, args.mode, args.mods, args.toolbox, args.assets)
            if not result["ready"]:
                code = 2
            if args.execute:
                if not result["ready"]:
                    raise ValueError("Build blocked: " + "; ".join(result["blockers"]))
                if not args.out:
                    raise ValueError("--execute requires --out")
                profile = read_json(args.profile)
                if profile.get("reconstruction"):
                    assets = args.assets or ROOT / profile["reconstruction"]["assets"]
                    result = execute_sample(args.base, args.donor, args.profile, assets, args.out,
                                            args.base_workspace, args.donor_workspace)
                else:
                    result = build_images(Path(args.base), Path(args.donor) if args.donor else None,
                                          args.profile, args.assets or ROOT / "assets/local", args.out, args.mods, args.toolbox)
        elif args.command == "apply":
            result = {"operations": apply_recipe(args.root, args.assets, read_json(args.recipe))}
        elif args.command == "repack-erofs":
            result = repack_erofs(args.tree, args.config, args.out, args.timestamp, args.uuid)
        elif args.command == "pack":
            result = package(args.source, args.out)
        elif args.command == "setup-tool":
            result = install_tool(args.id)
        elif args.command == "doctor":
            tools = {}
            for name in ("extract.erofs", "mkfs.erofs", "payload-dumper-go", "7z"):
                try:
                    tools[name] = str(executable(name))
                except ValueError:
                    tools[name] = None
            profile = read_json(ROOT / "profiles" / "pjf110-cph2745-oos16.json")
            recovered = profile["reconstruction"]
            try:
                asset_status = verify_assets(ROOT / recovered["recipe"], ROOT / recovered["assets"], content=False)
            except (ValueError, OSError) as exc:
                asset_status = {"status": "fail", "error": str(exc), "content_verified": False}
            result = {"python": sys.version.split()[0], "platform": sys.platform,
                      "tools": tools, "workspace_free_bytes": shutil.disk_usage(ROOT).free,
                      "profile_status": profile["status"], "assets": asset_status,
                      "profile_ready": profile["status"] in ("verified", "sample-verified") and asset_status["status"] == "pass",
                      "scope": "Tools resolved and sample assets checked for presence/size/link only; input availability, content and device validation require separate checks"}
        if getattr(args, "report", None):
            write_json(args.report, result)
            summary = {"report": str(args.report.resolve()), "status": result.get("status"),
                       "entries": len(result.get("entries", [])), "counts": result.get("counts"),
                       "ready": result.get("ready"), "blockers": result.get("blockers")}
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
