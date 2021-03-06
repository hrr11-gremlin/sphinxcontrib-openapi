"""
    sphinxcontrib.openapi
    ---------------------

    The OpenAPI spec renderer for Sphinx. It's a new way to document your
    RESTful API. Based on ``sphinxcontrib-httpdomain``.

    :copyright: (c) 2016, Ihor Kalnytskyi.
    :license: BSD, see LICENSE for details.
"""

import io
import itertools
import collections

import yaml
import jsonschema

from docutils import nodes
from docutils.parsers.rst import directives
from docutils.statemachine import ViewList

from sphinx.util.compat import Directive
from sphinx.util.nodes import nested_parse_with_titles

from sphinxcontrib import httpdomain


# Dictionaries do not guarantee to preserve the keys order so when we load
# JSON or YAML - we may loose the order. In most cases it's not important
# because we're interested in data. However, in case of OpenAPI spec it'd
# be really nice to preserve them since, for example, endpoints may be
# grouped logically and that improved readability.
class _YamlOrderedLoader(yaml.SafeLoader):
    pass


_YamlOrderedLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    lambda loader, node: collections.OrderedDict(loader.construct_pairs(node))
)


def _resolve_refs(uri, spec):
    """Resolve JSON references in a given dictionary.

    OpenAPI spec may contain JSON references to its nodes or external
    sources, so any attempt to rely that there's some expected attribute
    in the spec may fail. So we need to resolve JSON references before
    we use it (i.e. replace with referenced object). For details see:

        https://tools.ietf.org/html/draft-pbryan-zyp-json-ref-02

    The input spec is modified in-place despite being returned from
    the function.
    """
    resolver = jsonschema.RefResolver(uri, spec)

    def _do_resolve(node):
        for k, v in node.items():
            if isinstance(v, collections.Mapping) and '$ref' in v:
                with resolver.resolving(v['$ref']) as resolved:
                    node[k] = resolved
            elif isinstance(v, collections.Mapping):
                node[k] = _do_resolve(v)
            else:
                node[k] = v
        return node
    return _do_resolve(spec)


def _httpresource(endpoint, method, properties):
    parameters = properties.get('parameters', [])
    responses = properties['responses']
    indent = '   '

    yield '.. http:{0}:: {1}'.format(method, endpoint)
    yield '   :synopsis: {0}'.format(properties.get('summary', 'null'))
    yield ''

    if 'summary' in properties:
        for line in properties['summary'].splitlines():
            yield '{indent}**{line}**'.format(**locals())
        yield ''

    if 'description' in properties:
        for line in properties['description'].splitlines():
            yield '{indent}{line}'.format(**locals())
        yield ''

    # print request's route params
    for param in filter(lambda p: p['in'] == 'path', parameters):
        yield indent + ':param {type} {name}:'.format(**param)
        for line in param.get('description', '').splitlines():
            yield '{indent}{indent}{line}'.format(**locals())

    # print request's query params
    for param in filter(lambda p: p['in'] == 'query', parameters):
        yield indent + ':query {type} {name}:'.format(**param)
        for line in param.get('description', '').splitlines():
            yield '{indent}{indent}{line}'.format(**locals())

    # print response status codes
    for status, response in responses.items():
        yield '{indent}:status {status}:'.format(**locals())
        for line in response['description'].splitlines():
            yield '{indent}{indent}{line}'.format(**locals())

    # print request header params
    for param in filter(lambda p: p['in'] == 'header', parameters):
        yield indent + ':reqheader {name}:'.format(**param)
        for line in param.get('description', '').splitlines():
            yield '{indent}{indent}{line}'.format(**locals())

    # print response headers
    for status, response in responses.items():
        for headername, header in response.get('headers', {}).items():
            yield indent + ':resheader {name}:'.format(name=headername)
            for line in header['description'].splitlines():
                yield '{indent}{indent}{line}'.format(**locals())

    yield ''


def string_multiline_list(value):
    """ Clean a string option by converting it to a list of strings

    Split string on newlines to get list of paths to filter by

    Returns an array of string paths to filter on
    Raises ValueError if unable to parse the input value

    """
    paths = [path.strip() for path in value.splitlines()]
    if len(paths) > 0:
        return paths
    else:
        raise ValueError('Invalid argument to paths param: {}'.format(value))


def openapi2httpdomain(spec, **options):
    generators = []

    # If we don't pass a specific list of paths to render,
    # default to all in spec
    paths = options.get('paths', spec['paths'])

    for endpoint in paths:
        try:
            path = spec['paths'][endpoint]
        except KeyError:
            error_msg = ('Invalid path filter \'{}\' in OpenAPI directive. ' +
                         'Path must be one of {}.')
            raise ValueError(error_msg.format(endpoint, spec['paths'].keys()))
        for method, properties in path.items():
            generators.append(_httpresource(endpoint, method, properties))

    return iter(itertools.chain(*generators))


class OpenApi(Directive):

    required_arguments = 1                  # path to openapi spec
    final_argument_whitespace = True        # path may contain whitespaces
    option_spec = {
        'encoding': directives.encoding,    # useful for non-ascii cases :)
        'paths': string_multiline_list,     # Filter output based on the
                                            # list of paths provided
    }

    def run(self):
        env = self.state.document.settings.env
        rel_path, path = env.relfn2path(directives.path(self.arguments[0]))

        # Add OpenAPI spec as a dependency to the current document. That means
        # the document will be rebuilt if the spec is changed.
        env.note_dependency(rel_path)

        # Read the spec using encoding passed to the directive or fallback to
        # the one specified in Sphinx's config.
        encoding = self.options.get('encoding', env.config.source_encoding)
        with io.open(path, 'rt', encoding=encoding) as stream:
            spec = yaml.load(stream, _YamlOrderedLoader)

        # OpenAPI spec may contain JSON references, so we need resolve them
        # before we access the actual values trying to build an httpdomain
        # markup. Since JSON references may be relative, it's crucial to
        # pass a document URI in order to properly resolve them.
        spec = _resolve_refs('file://%s' % path, spec)

        # reStructuredText DOM manipulation is pretty tricky task. It requires
        # passing dozen arguments which is not easy without well-documented
        # internals. So the idea here is to represent OpenAPI spec as
        # reStructuredText in-memory text and parse it in order to produce a
        # real DOM.
        viewlist = ViewList()
        for line in openapi2httpdomain(spec, **self.options):
            viewlist.append(line, '<openapi>')

        # Parse reStructuredText contained in `viewlist` and return produced
        # DOM nodes.
        node = nodes.section()
        node.document = self.state.document
        nested_parse_with_titles(self.state, viewlist, node)
        return node.children


def setup(app):
    if 'http' not in app.domains:
        httpdomain.setup(app)
    app.add_directive('openapi', OpenApi)
