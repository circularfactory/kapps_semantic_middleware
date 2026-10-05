# This package exports `SemanticMiddleware` and `Mode`, and nothing else.
#
# It used to re-export thirteen names from `transitional_sync_middleware` as well -- `Middleware`,
# `AasMiddleware`, `Reference`, `Identifier`, `DataModel`, `DataModelRebuilder`, `AAS`, `Submodel`,
# `SubmodelElementCollection`, `Blob`, `File`, `formatting` and `connectors` -- on the plan that
# each would be swapped for a local implementation as its layer was migrated. Nothing was ever
# swapped, and nothing ever read them from here: every module in this package, every example,
# scenario and test that needs a base class or a datamodel imports it from
# `transitional_sync_middleware` by its own path.
#
# Seven of the thirteen were the AAS data model and its persistence layer -- `AasMiddleware`, `AAS`,
# `Submodel`, `SubmodelElementCollection`, `Blob`, `File` and `formatting`, the last of them a
# package of AAS and Basyx formatters. This middleware exists to not have them, so a consumer who
# found them on our namespace found the opposite of what this library claims about itself.
# `Reference` and `Identifier` were `TypeVar`s, which carry no meaning outside the module that
# binds them. And `connectors` never resolved to the re-exported module at all: this package has a
# subpackage of that name, and importing it rebinds the attribute, so `__all__` promised one module
# and delivered another.
#
# A name belongs here when this package or a documented consumer imports it from here. That is the
# test an addition has to pass.
from kapps_semantic_middleware.middleware import SemanticMiddleware
from kapps_semantic_middleware.modes import Mode

__all__ = [
    "SemanticMiddleware",
    "Mode",
]
