"""Load and link .pyi files."""

import logging
import os


from pytype.pytd import pep484
from pytype.pytd import typeshed
from pytype.pytd import utils as pytd_utils
from pytype.pytd.parse import builtins
from pytype.pytd.parse import visitors

log = logging.getLogger(__name__)


class Module(object):
  """Represents a parsed module.

  Attributes:
    module_name: The module name, e.g. "numpy.fft.fftpack".
    filename: The filename of the pytd that describes the module. Needs to be
      unique.
    ast: The parsed PyTD. Internal references will be resolved, but
      NamedType nodes referencing other modules might still be unresolved.
  """

  def __init__(self, module_name, filename, ast):
    self.module_name = module_name
    self.filename = filename
    self.ast = ast
    self.dirty = True


class DependencyNotFoundError(Exception):
  """If we can't find a module referenced by the module we're trying to load."""

  def __init__(self, module_name):
    super(DependencyNotFoundError, self).__init__("Can't find %s" % module_name)
    self.module_name = module_name


class Loader(object):
  """A cache for loaded PyTD files.

  Typically, you'll have one instance of this class, per module.

  Attributes:
    base_module: The full name of the module we're based in (i.e., the module
      that's importing other modules using this loader).
    options: config.Options object
    _modules: A map, filename to Module, for caching modules already loaded.
    _concatenated: A concatenated pytd of all the modules. Refreshed when
                   necessary.
  """

  PREFIX = "pytd:"  # for pytd files that ship with pytype

  def __init__(self,
               base_module,
               options):
    self.base_module = base_module
    self.options = options
    self.builtins, self.typing = builtins.GetBuiltinsAndTyping()
    self._modules = {
        "__builtin__":
        Module("__builtin__", self.PREFIX + "__builtin__", self.builtins),
        "typing":
        Module("typing", self.PREFIX + "typing", self.typing)
    }
    self._concatenated = None
    # Paranoid verification that pytype.main properly checked the flags:
    if self.options.imports_map is not None:
      assert self.options.pythonpath == [""]

  def _postprocess_pyi(self, ast):
    """Apply all the PYI transformations we need."""
    ast = ast.Visit(pep484.ConvertTypingToNative(self.options.python_version))
    ast = ast.Visit(visitors.SimplifyOptionalParameters())
    ast = ast.Visit(visitors.LookupBuiltins(self.builtins))
    ast = ast.Visit(visitors.NamedTypeToClassType())
    return ast

  def _create_empty(self, module_name, filename):
    return self._load_file(module_name, filename,
                           pytd_utils.EmptyModule(module_name))

  def _load_file(self, module_name, filename, ast=None):
    """Load (or retrieve from cache) a module and resolve its dependencies."""
    self._concatenated = None  # invalidate
    existing = self._modules.get(module_name)
    if existing:
      if existing.filename != filename:
        raise AssertionError("%s exists as both %s and %s" %
                             (module_name, filename, existing.filename))
      return existing.ast
    if not ast:
      ast = builtins.ParsePyTD(filename=filename,
                               module=module_name,
                               python_version=self.options.python_version)
    ast = self._postprocess_pyi(ast)
    module = Module(module_name, filename, ast)
    self._modules[module_name] = module
    try:
      module.ast = self._load_and_resolve_ast_dependencies(module.ast,
                                                           module_name)
      # Insert templates after resolving any imported TypeVar instances
      module.ast = module.ast.Visit(visitors.InsertSignatureTemplates())
      # All external ClassType nodes have been resolved, but internal ones are
      # unresolved, so we adjust templates in the module using itself as a
      # lookup table.
      module.ast = module.ast.Visit(pytd_utils.AdjustTemplates(module.ast))
      # Now we can fill in the remaining cls pointers. This code executes when
      # the module is first loaded, which happens before any others use it to
      # resolve dependencies, so there are no external pointers into the module
      # at this point.
      module.ast.Visit(
          visitors.FillInModuleClasses({"": ast, module_name: ast}))
    except:
      del self._modules[module_name]  # don't leave half-resolved modules around
      raise
    return module.ast

  def _load_and_resolve_ast_dependencies(self, ast, ast_name=None):
    """Fill in all ClassType.cls pointers."""
    deps = visitors.CollectDependencies()
    ast.Visit(deps)
    if deps.modules:
      for name in deps.modules:
        if name not in self._modules:
          other_ast = self._import_name(name)
          if other_ast is None:
            raise DependencyNotFoundError(name)
      module_map = {name: module.ast
                    for name, module in self._modules.items()}
      ast = ast.Visit(visitors.LookupExternalTypes(module_map, full_names=True,
                                                   self_name=ast_name))
    return ast

  def _finish_ast(self, ast):
    module_map = {name: module.ast
                  for name, module in self._modules.items()}
    module_map[""] = ast  # The module itself (local lookup)
    ast.Visit(visitors.FillInModuleClasses(module_map))
    ast.Visit(visitors.VerifyLookup())
    ast.Visit(visitors.VerifyContainers())

  def resolve_ast(self, ast):
    """Resolve the dependencies of an AST, without adding it to our modules."""
    ast = self._postprocess_pyi(ast)
    ast = self._load_and_resolve_ast_dependencies(ast)
    self._lookup_all_classes()
    self._finish_ast(ast)
    return ast

  def _lookup_all_classes(self):
    for module in self._modules.values():
      if module.dirty:
        self._finish_ast(module.ast)
        module.dirty = False

  def import_relative_name(self, name):
    """IMPORT_NAME with level=-1. A name relative to the current directory."""
    if self.base_module is None:
      raise ValueError("Attempting relative import in non-package.")
    path = self.base_module.split(".")[:-1]
    path.append(name)
    ast = self._import_name(".".join(path))
    self._lookup_all_classes()
    return ast

  def import_relative(self, level):
    """Import a module relative to our base module.

    Args:
      level: Relative level:
        https://docs.python.org/2/library/functions.html#__import__
        E.g.
          1: "from . import abc"
          2: "from .. import abc"
          etc.
        Since you'll use import_name() for -1 and 0, this function expects the
        level to be >= 1.
    Returns:
      The parsed pytd. Instance of pytd.TypeDeclUnit. None if we can't find the
      module.
    Raises:
      ValueError: If we don't know the name of the base module.
    """
    assert level >= 1
    if self.base_module is None:
      raise ValueError("Attempting relative import in non-package.")
    components = self.base_module.split(".")
    sub_module = ".".join(components[0:-level])
    ast = self._import_name(sub_module)
    self._lookup_all_classes()
    return ast

  def import_name(self, module_name):
    ast = self._import_name(module_name)
    self._lookup_all_classes()
    return ast

  def _load_builtin(self, subdir, module_name):
    """Load a pytd/pyi that ships with pytype or typeshed."""
    version = self.options.python_version
    # Try our own type definitions first.
    mod = builtins.ParsePredefinedPyTD(subdir, module_name, version)
    if not mod and self.options.typeshed:
      # Fall back to typeshed.
      mod = typeshed.parse_type_definition(subdir, module_name, version)
    if mod:
      log.debug("Found %s entry for %r", subdir, module_name)
      return self._load_file(filename=self.PREFIX + module_name,
                             module_name=module_name,
                             ast=mod)
    return None

  def _import_name(self, module_name):
    """Load a name like 'sys' or 'foo.bar.baz'.

    Args:
      module_name: The name of the module. May contain dots.

    Returns:
      The parsed file, instance of pytd.TypeDeclUnit, or None if we
      the module wasn't found.
    """
    assert os.sep not in module_name, (os.sep, module_name)
    log.debug("Trying to import %r", module_name)
    # Builtin modules (but not standard library modules!) take precedence
    # over modules in PYTHONPATH.
    mod = self._load_builtin("builtins", module_name)
    if mod:
      return mod

    file_ast = self._import_file(module_name, module_name.split("."))
    if file_ast:
      return file_ast

    # The standard library is (typically) at the end of PYTHONPATH.
    mod = self._load_builtin("stdlib", module_name)
    if mod:
      return mod

    log.warning("Couldn't import module %s %r in (path=%r) imports_map: %s",
                module_name, module_name, self.options.pythonpath,
                "%d items" % len(self.options.imports_map) if
                self.options.imports_map else "none")
    if log.isEnabledFor(logging.DEBUG) and self.options.imports_map:
      for module, path in self.options.imports_map.items():
        log.debug("%s -> %s", module, path)
    return None

  def _import_file(self, module_name, module_name_split):
    """Helper for import_relative: try to load an AST, using pythonpath.

    Loops over self.options.pythonpath, taking care of the semantics for
    __init__, and pretending there's an empty __init__ if the path (derived from
    module_name_split) is a directory.

    Args:
      module_name: The name of the module. May contain dots.
      module_name_split: module_name.split(".")
    Returns:
      The parsed file (AST) if found, otherwise None.

    """
    for searchdir in self.options.pythonpath:
      path = os.path.join(searchdir, *module_name_split)
      # See if this is a directory with a "__init__.py" defined.
# MOE:strip_line For Bazel, have already created a __init__.py file
      init_path = os.path.join(path, "__init__")
      init_ast = self._load_pyi(init_path, module_name)
      if init_ast is not None:
        log.debug("Found module %r with path %r", module_name, init_path)
        return init_ast
      elif os.path.isdir(path):
        # We allow directories to not have an __init__ file.
        # The module's empty, but you can still load submodules.
        # TODO(pludemann): remove this? - it's not standard Python.
        log.debug("Created empty module %r with path %r",
                  module_name, init_path)
        return self._create_empty(filename=os.path.join(path, "__init__.pyi"),
                                  module_name=module_name)
      else:  # Not a directory
        file_ast = self._load_pyi(path, module_name)
        if file_ast is not None:
          log.debug("Found module %r in path %r", module_name, path)
          return file_ast
    return None

  def _load_pyi(self, path, module_name):
    """Load a pyi from the path.

    Args:
      path: Path to the file (without '.pyi' or similar extension).
      module_name: Name of the module (may contain dots).
    Returns:
      The parsed pyi, instance of pytd.TypeDeclUnit, or None if we didn't
      find the module.
    """
    if self.options.imports_map is not None:
      if path in self.options.imports_map:
        full_path = self.options.imports_map[path]
      else:
        return None
    else:
      full_path = path + ".pyi"
    # We have /dev/null entries in the import_map - os.path.isfile() returns
    # False for those. However, we *do* want to load them. Hence exists / isdir.
    if os.path.exists(full_path) and not os.path.isdir(full_path):
      return self._load_file(filename=full_path, module_name=module_name)
    else:
      return None

  def concat_all(self):
    if not self._concatenated:
      self._concatenated = pytd_utils.Concat(
          *(module.ast for module in self._modules.values()),
          name="<all>")
    return self._concatenated
