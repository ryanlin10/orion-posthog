"""The recipes people start from: the shipped file, the copy the setup skill writes, and the skills' examples all compile,
and the shipped file and the setup skill never drift apart."""
import json
import re
import unittest
from pathlib import Path

import yaml

from paradigm_orion.dataset.config import recipe_config
from paradigm_orion.recipe import builder_recipe, compile_recipe, load_recipe

ROOT = Path(__file__).resolve().parents[2]


def yaml_blocks(path):
    return re.findall(r"```yaml\n(.*?)```", (ROOT / path).read_text(), re.S)


class RecipeTests(unittest.TestCase):
    def test_shipped_recipes_compile_for_the_builder(self):
        files = sorted((ROOT / "recipes").glob("*.yaml"))
        self.assertEqual([f.name for f in files], ["site-feelings.yaml"])
        for path in files:
            recipe = load_recipe(path)
            recipe_config(builder_recipe(recipe))

    def test_the_setup_skill_writes_the_shipped_recipe(self):
        shipped = "".join(line for line in (ROOT / "recipes" / "site-feelings.yaml").read_text().splitlines(keepends=True) if not line.startswith("#"))
        written = next(b for b in yaml_blocks("skills/orion-setup/SKILL.md") if b.startswith("name: site-feelings"))
        self.assertEqual(written, shipped)

    def test_skill_examples_compile(self):
        reference = next(b for b in yaml_blocks("skills/orion-datasets/SKILL.md") if b.startswith("name:"))
        recipe_config(builder_recipe(compile_recipe(yaml.safe_load(reference))))
        outcome = next(b for b in yaml_blocks("skills/orion-datasets/SKILL.md") if "outcome:" in b)
        compiled = compile_recipe({"name": "checkout-outcome", **yaml.safe_load(outcome)})
        self.assertEqual(compiled["labels"]["outcome"]["otherwise"], "left")

    def test_the_minimal_builder_recipe_in_the_guide_is_valid(self):
        block = re.search(r"A minimal recipe:\n\n```json\n(.*?)\n```", (ROOT / "docs" / "training.md").read_text(), re.S).group(1)
        recipe_config(json.loads(block))


if __name__ == "__main__":
    unittest.main()
