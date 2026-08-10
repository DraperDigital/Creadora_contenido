// Ensures src/Compositions.generated.ts exists so the committed Remotion entry
// (src/index.ts -> Root.tsx) resolves on a fresh clone.
//
// Compositions.generated.ts is an intentionally gitignored build artifact.
// Real renders never touch it: the Python orchestrators generate a per-render,
// ISOLATED manifest at a unique path (see shared/remotion_manifest_writer.py /
// remotion_manifest_short.py plus render_remotion.render_isolated_composition),
// so this file only backs `npm run remotion:studio` / `remotion:render` and
// Remotion Studio on a checkout that hasn't generated a run yet. This shim
// writes a safe, empty default when the file is missing and never overwrites
// an existing one.
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { existsSync, writeFileSync } from "node:fs";

const here = dirname(fileURLToPath(import.meta.url));
const target = join(here, "src", "Compositions.generated.ts");

if (existsSync(target)) {
  console.log("ensure-compositions: Compositions.generated.ts already present");
} else {
  const body = [
    "// AUTO-GENERATED placeholder written by ensure-compositions.mjs.",
    "// Do not edit by hand.",
    'import type React from "react";',
    'import type { CompositionEntry } from "./lib/types";',
    "",
    "export const COMPOSITIONS: ReadonlyArray<",
    "  CompositionEntry & { component: React.FC<any> }",
    "> = [];",
    "",
  ].join("\n");
  writeFileSync(target, body, "utf8");
  console.log("ensure-compositions: wrote empty placeholder Compositions.generated.ts");
}
