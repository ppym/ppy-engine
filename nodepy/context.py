"""
Concrete implementation of the Node.py runtime.
"""

from nodepy import base, extensions, loader, resolver, utils
from nodepy.utils import pathlib
import contextlib
import localimport
import os
import six
import sys


class Require(object):
  """
  Implements the `require` object that is available to Node.py modules.
  """

  ResolveError = base.ResolveError

  def __init__(self, context, directory):
    assert isinstance(context, Context)
    assert isinstance(directory, pathlib.Path)
    self.context = context
    self.directory = directory
    self.path = []
    self.cache = {}

  def __call__(self, request, exports=True):
    module = self.resolve(request)
    if not module.loaded:
      self.context.load_module(module)
    if exports:
      if module.exports is NotImplemented:
        return module.namespace
      return module.exports
    else:
      return module

  def resolve(self, request):
    request = utils.as_text(request)
    module = self.cache.get(request)
    if not module or module.exception:
      module = self.context.resolve(request, self.directory, self.path)
      self.cache[request] = module
    return module

  def star(self, request, symbols=None):
    """
    Performs a 'star' import into the parent frame.
    """

    if isinstance(symbols, str):
      if ',' in symbols:
        symbols = [x.strip() for x in symbols.split(',')]
      else:
        symbols = symbols.split()

    into = sys._getframe(1).f_locals
    namespace = self(request)

    if symbols is None:
      symbols = getattr(namespace, '__all__', None)
    if symbols is None:
      for key in dir(namespace):
        if not key.startswith('_') and key not in ('module', 'require'):
          into[key] = getattr(namespace, key)
    else:
      for key in symbols:
        into[key] = getattr(namespace, key)

  def try_(self, *requests, load=True, exports=True):
    """
    Load every of the specified *requests* until the first can be required
    without error. Only if the requested module can not be found will the
    next module be tried, otherwise the error will be propagated.

    If none of the requests match, the last #ResolveError will be re-raised.
    If *requests* is empty, a #ValueError is raised.
    """

    exc_info = None
    for request in requests:
      try:
        if load:
          return self(request, exports=exports)
        else:
          return self.resolve(request)
      except self.ResolveError as exc:
        exc_info = sys.exc_info()
        if exc.request.string != request:
          raise

    if exc_info:
      raise six.reraise(*exc_info)
    raise ValueError('no requests specified')

  @property
  def main(self):
    return self.context.main_module

  @property
  def current(self):
    return self.context.current_module

  def breakpoint(self, tb=None, stackdepth=0):
    """
    Enters the interactive debugger. If *tb* is specified, the debugger will
    be entered at the specified traceback. *tb* may also be the value #True
    in which case `sys.exc_info()[2]` is used as the traceback.

    The `NODEPY_BREAKPOINT` environment variable will be considered to
    determine the implementation of the debugger. If it is an empty string
    or unset, #Context.breakpoint() will be called. If it is `0`, the
    function will return `None` immediately without invoking a debugger.
    Otherwise, it must be a string that can be `require()`-ed from the
    current working directory. The loaded module's `breakpoint()` function
    will be called with *tb* as single parameter.
    """

    if tb is True:
      tb = sys.exc_info()[2]
      if not tb:
        raise RuntimeError('no current exception information')

    var = os.getenv('NODEPY_BREAKPOINT', '')
    if var == '0':
      return

    if var:
      # XXX Use Context.require once it is implemented.
      self.context.require(var).breakpoint(tb, stackdepth+1)
    else:
      self.context.breakpoint(tb, stackdepth+1)

  def new(self, directory):
    """
    Creates a new #Require instance for the specified *directory*.
    """

    if isinstance(directory, str):
      directory = pathlib.Path(directory)
    return type(self)(self.context, directory)


class Context(object):

  modules_directory = '.nodepy_modules'
  package_manifest = 'nodepy.json'
  package_main = 'index'
  link_file = '.nodepy-link.txt'

  def __init__(self, bare=False, maindir=None):
    self.maindir = maindir or pathlib.Path.cwd()
    self.require = Require(self, self.maindir)
    self.extensions = []
    self.resolvers = []
    self.modules = {}
    self.packages = {}
    self.module_stack = []
    self.main_module = None
    self.localimport = localimport.localimport([])
    if not bare:
      loaders = [loader.PythonLoader(), loader.PackageRootLoader()]
      std_resolver = resolver.StdResolver([], loaders)
      self.resolvers.append(std_resolver)
      self.extensions.append(extensions.ImportSyntax())

  @contextlib.contextmanager
  def enter(self, isolated=False):
    """
    Returns a context-manager that enters and leaves this context. If
    *isolated* is #True, the #localimport module will be used to restore
    the previous global importer state when the context is exited.

    > Note: This method reloads the #pkg_resources module on entering and
    > exiting the context. This is necessary to update the state of the
    > module for the updated global importer state.
    """

    @contextlib.contextmanager
    def reload_pkg_resources():
      utils.machinery.reload_pkg_resources()
      yield
      if isolated:
        utils.machinery.reload_pkg_resources()

    @contextlib.contextmanager
    def activate_localimport():
      self.localimport.__enter__()
      yield
      if isolated:
        self.localimport.__exit__()

    with utils.context.ExitStack() as stack:
      stack.add(activate_localimport())
      stack.add(reload_pkg_resources())
      sys.path_importer_cache.clear()
      yield

  def resolve(self, request, directory=None, additional_search_path=()):
    if not isinstance(request, base.Request):
      if directory is None:
        directory = pathlib.Path.cwd()
      request = base.Request(self, directory, request, additional_search_path)

    search_paths = []
    for resolver in self.resolvers:
      try:
        module = resolver.resolve_module(request)
      except base.ResolveError as exc:
        assert exc.request is request, (exc.request, request)
        search_paths.extend(exc.search_paths)
        continue

      if not isinstance(module, base.Module):
        msg = '{!r} returned non-Module object {!r}'
        msg = msg.format(type(resolver).__name__, type(module).__name__)
        raise RuntimeError(msg)
      have_module = self.modules.get(module.filename)
      if have_module is not None and have_module is not module:
        msg = '{!r} returned new Module object besides an existing entry '\
              'in the cache'.format(type(resolver).__name__)
        raise RuntimeError(msg)
      self.modules[module.filename] = module
      return module

    raise base.ResolveError(request, search_paths)

  def load_module(self, module, do_init=True):
    """
    This method should be the preferred way to call #Module.load() as it
    performs integrity checks and keeps track of the module in the
    #Context.module_stack list.

    If loading the module resulted in an exception before and it is still
    stored in #Module.exception, it is re-raised.
    """

    assert isinstance(module, base.Module)
    if module.exception:
      six.reraise(*module.exception)
    if module.loaded:
      return
    if module.filename not in self.modules:
      msg = '{!r} can not be loaded when not in Context.modules'
      raise RuntimeError(msg.format(module))

    if do_init:
      module.init()
    self.module_stack.append(module)
    try:
      module.load()
    except:
      module.exception = sys.exc_info()
      del self.modules[module.filename]
      raise
    else:
      module.loaded = True
    finally:
      if self.module_stack.pop() is not module:
        raise RuntimeError('Context.module_stack corrupted')

  @property
  def current_module(self):
    if self.module_stack:
      return self.module_stack[-1]
    return None

  @contextlib.contextmanager
  def push_main(self, module):
    """
    A context-manager to temporarily shadow the #Context.main_module with
    the specified *module*.
    """

    if not isinstance(module, base.Module):
      raise TypeError('expected nodepy.base.Module instance')

    prev_module = self.main_module
    self.main_module = module
    try:
      yield
    finally:
      self.main_module = prev_module

  def __breakpoint__(self, tb=None, stackdepth=0):
    """
    Default implementation of the #breakpoint() method. Uses PDB.
    """

    if tb is not None:
      utils.FrameDebugger().interaction(None, tb)
    else:
      frame = sys._getframe(stackdepth+1)
      utils.FrameDebugger().set_trace(frame)

  def breakpoint(self, tb=None, stackdepth=0):
    """
    The default implementation of this method simply calls #__breakpoint__().
    It can be overwritten (eg. simply by setting the member on the #Context
    object) to alter the behaviour. This method is called by
    #Require.breakpoint().
    """

    self.__breakpoint__(tb, stackdepth+1)
