"""Typed, versioned domain models for RICH.

This package depends only on the Python standard library. These models are the
boundary between product intent, the architecture compiler, and durable
execution state, so they validate eagerly, serialize to plain JSON-compatible
dictionaries, and reject unknown fields when loading.

It is split by subject rather than kept as one file, but it is imported as one
name: every symbol below is re-exported here, so callers write
``from .models import ProjectSpec`` and never need to know which module a type
lives in. The layering runs one way -- common → spec/types → contracts →
architecture → runs -- and a test holds the package to importing nothing from
the rest of richbuild.
"""

from ._common import *  # noqa: F401,F403
from .spec import *  # noqa: F401,F403
from .types import *  # noqa: F401,F403
from .contracts import *  # noqa: F401,F403
from .architecture import *  # noqa: F401,F403
from .runs import *  # noqa: F401,F403
