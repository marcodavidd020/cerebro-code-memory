from cerebro import db, graph, indexer, tsconfig
from cerebro.config import Config

from conftest import make_config


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_jsonc_tolerates_comments_and_trailing_commas():
    text = """
    {
      // a line comment
      "compilerOptions": {
        /* block comment */
        "baseUrl": ".",
        "paths": { "@/*": ["./src/*"], },
      },
    }
    """
    data = tsconfig._loads_jsonc(text)
    assert data["compilerOptions"]["paths"]["@/*"] == ["./src/*"]


def test_jsonc_does_not_treat_globs_as_comments():
    # regression: "@/*" contains /* and "**/*.ts" contains */ — a naive regex
    # stripper deletes everything between them and corrupts the JSON.
    text = """
    {
      "compilerOptions": { "paths": { "@/*": ["./*"] } },
      "include": ["**/*.ts", ".next/types/**/*.ts"]
    }
    """
    data = tsconfig._loads_jsonc(text)
    assert data["compilerOptions"]["paths"]["@/*"] == ["./*"]
    assert "**/*.ts" in data["include"]


def test_match_wildcard_and_exact():
    assert tsconfig._match("@/*", "@/a/b") == "a/b"
    assert tsconfig._match("@/*", "other") is None
    assert tsconfig._match("@app", "@app") == ""
    assert tsconfig._match("@app", "@apple") is None


def test_expand_uses_nearest_config_and_baseurl(tmp_path):
    config = make_config(tmp_path)
    write(tmp_path, "tsconfig.json", '{"compilerOptions":{"baseUrl":".","paths":{"@root/*":["./*"]}}}')
    write(
        tmp_path,
        "app/tsconfig.json",
        '{"compilerOptions":{"baseUrl":".","paths":{"@/*":["./src/*"]}}}',
    )
    configs = tsconfig.load_alias_configs(config)
    # nearest config for a file under app/ is app/tsconfig.json
    cands = tsconfig.expand("@/components/Button", "app/pages/home.tsx", configs)
    assert "app/src/components/Button" in cands
    # root config still applies to files at the root
    cands_root = tsconfig.expand("@root/lib/x", "index.ts", configs)
    assert "lib/x" in cands_root


def test_extends_inherits_paths(tmp_path):
    config = make_config(tmp_path)
    write(
        tmp_path,
        "tsconfig.base.json",
        '{"compilerOptions":{"baseUrl":".","paths":{"@/*":["./src/*"]}}}',
    )
    write(tmp_path, "tsconfig.json", '{"extends":"./tsconfig.base.json"}')
    configs = tsconfig.load_alias_configs(config)
    cands = tsconfig.expand("@/utils/x", "main.ts", configs)
    assert "src/utils/x" in cands


def test_reindex_resolves_alias_edges(tmp_path, project):
    config, conn = project
    write(tmp_path, "tsconfig.json", '{"compilerOptions":{"baseUrl":".","paths":{"@/*":["./src/*"]}}}')
    write(tmp_path, "src/utils/helper.ts", "export function helper() { return 1; }\n")
    write(
        tmp_path,
        "src/pages/home.tsx",
        "import { helper } from '@/utils/helper';\nexport const Home = () => helper();\n",
    )
    indexer.reindex(config, conn)
    edges = {(r["src_path"], r["dst_path"]) for r in conn.execute("SELECT src_path, dst_path FROM edges")}
    # the @/ aliased import must resolve to a concrete file
    assert ("src/pages/home.tsx", "src/utils/helper.ts") in edges
    assert graph.dependents(conn, "src/utils/helper.ts") == ["src/pages/home.tsx"]


def test_cerebroignore_excludes_paths(tmp_path, project):
    config, conn = project
    write(tmp_path, "keep.ts", "export const a = 1;\n")
    write(tmp_path, "backup/old.ts", "export const b = 2;\n")
    write(tmp_path, ".cerebroignore", "backup/\n")
    # reload config so it picks up the .cerebroignore we just wrote
    config = Config.load(start=str(tmp_path))
    indexer.reindex(config, conn)
    assert set(db.stored_hashes(conn)) == {"keep.ts"}
