"""Local, recipe-driven telemetry dataset construction. No model or network dependencies."""

__version__ = "0.2.0"


def build_dataset(recipe, batches, labels, recordings, output):
    from .pipeline import build_dataset as build
    return build(recipe, batches, labels, recordings, output)
