"""What the agent this plugin is loaded into calls itself.

A rebranded build of the agent renames its own modules, so the name is read off a core module
rather than written here: every name this plugin shows Anthropic or the user follows the host.
"""


def _name() -> str:
    try:
        import nako_constants
    except ImportError:  # the flat test path, with no core on sys.path
        return 'nako'
    return nako_constants.__name__.partition('_')[0]


NAME = _name()
TITLE = NAME.capitalize()
