"""对某个 World 注入/拆除配方。"""

from app.services.compiler.primitives import apply_primitive
from app.services.compiler.schema import Recipe


def apply_recipe(docker, recipe: Recipe) -> dict:
    outputs = [apply_primitive(docker, item, undo=False) for item in recipe.primitives]
    if len(outputs) == 1:
        return outputs[0]
    return {"operation": "recipe", "recipe_id": recipe.id, "outputs": outputs}


def undo_recipe(docker, recipe: Recipe) -> dict:
    outputs = [apply_primitive(docker, item, undo=True) for item in reversed(recipe.primitives)]
    if len(outputs) == 1:
        return outputs[0]
    return {"operation": "recipe_undo", "recipe_id": recipe.id, "outputs": outputs}
