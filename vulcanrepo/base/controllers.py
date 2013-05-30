import os
import json
import logging
import tempfile
import urllib
import cgi

from ming.odm import session
from vulcanforge.common.validators import DateTimeConverter
from webob import exc
from formencode import validators
from paste.deploy.converters import asbool
from pylons import tmpl_context as c, app_globals as g, request, response
from tg import redirect, expose, flash, validate
from tg.decorators import with_trailing_slash, without_trailing_slash
from boto.exception import S3ResponseError
from vulcanforge.common.controllers import BaseController
from vulcanforge.common.controllers.decorators import require_post
from vulcanforge.common import helpers as h
from vulcanforge.common.util import (
    set_cache_headers,
    set_download_headers,
    push_config
)
from vulcanforge.artifact.controllers import (
    ArtifactRestController,
    BaseAlternateRestController
)
from vulcanforge.artifact.model import Feed, ArtifactReference
from vulcanforge.artifact.widgets import RelatedArtifactsWidget
from vulcanforge.cache.decorators import cache_rendered
from vulcanforge.discussion.controllers import AppDiscussionController
import vulcanforge.discussion.widgets
from vulcanforge.project.model import Project
from vulcanforge.stats import STATS_CACHE_TIMEOUT
from vulcanforge.visualize.model import Visualizer
from vulcanforge.visualize.widgets.visualize import (
    DiffVisualizer,
    ArtifactEmbedVisualizer
)

from vulcanrepo import tasks as repo_tasks
from vulcanrepo.stats import CommitAggregator, CommitQuerySchema
from .model import Commit
from .widgets import (
    SCMLogWidget,
    SCMCommitWidget,
    SCMCommitBrowserWidget,
    CommitAuthorWidget
)

LOG = logging.getLogger(__name__)
TEMPLATE_DIR = 'jinja:vulcanrepo.base:templates/'


# Methods for retrieving repo artifacts from the request
def get_commit(rev, args, depth=10):
    commit = c.app.repo.commit(rev)
    if not commit:
        if not args or not depth:
            raise exc.HTTPNotFound()
        return get_commit('{}/{}'.format(rev, args[0]), args[1:], depth - 1)
    return commit, rev, args


def get_path(args, use_ext=False):
    path = '/' + '/'.join(args)
    if use_ext:
        request_ext = request.response_ext
        if request_ext and not path.endswith(request_ext):
            path += request.response_ext
    return path


def get_commit_and_obj(rev, *args, **kw):
    """Get commit and file/folder object from rev and args"""
    commit, rev, args = get_commit(rev, args)
    path = get_path(args, kw.get('use_ext', False))
    obj = commit.get_path(path)
    if not obj:
        raise exc.HTTPNotFound()
    return commit, obj, rev


class S3ProxyController(BaseController):
    """Temporary until we figure out how to effectively serve static files"""

    @expose()
    def resource(self, *args, **kw):
        g.security.require_access(c.app, 'read')
        key_name = get_path(map(h.urlquote, args), use_ext=True)
        LOG.info('getting repo s3 key at %s', key_name)
        try:
            key = g.s3_bucket.get_key(key_name)
        except S3ResponseError:
            LOG.warn('Not found -- Key: %s', key_name)
            raise exc.HTTPNotFound
        if key is None:
            LOG.warn('Key %s not found', key_name)
            raise exc.HTTPNotFound
        set_cache_headers(expires_in=14)
        response.headers['Content-Type'] = ''
        response.content_type = key.content_type.encode('utf-8')
        return iter(key)


class RepoStatsController(BaseController):

    @expose(TEMPLATE_DIR + 'stats/index.html')
    def index(self):
        return {
            'title': '{} Statistics'.format(c.app.config.options.mount_label),
            'data_src': '{}stats/commit_aggregate'.format(c.app.url)
        }

    @expose('json')
    @cache_rendered(timeout=STATS_CACHE_TIMEOUT)
    @validate(CommitQuerySchema())
    def commit_aggregate(self, date_start=None, date_end=None, bins=None,
                         order=None, label=None, user=None, **extra_query):
        if bins is None:
            bins = ['daily']
        agg = CommitAggregator(
            date_start=date_start,
            date_end=date_end,
            bins=bins,
            repo=c.app.repo,
            order=order,
            label=label,
            user=user,
            extra_query=extra_query
        )
        agg.run()
        return agg.fix_results()


class BaseRepositoryController(BaseController):
    _s3_proxy = S3ProxyController()
    stats = RepoStatsController()

    class Widgets(BaseController.Widgets):
        commit_browser_widget = SCMCommitBrowserWidget()
        commit_author_widget = CommitAuthorWidget()
        diff_widget = DiffVisualizer()
        visualizer_widget = ArtifactEmbedVisualizer()
        log_widget = SCMLogWidget()
        thread_widget = vulcanforge.discussion.widgets.ThreadWidget(
            page=None, limit=None, page_size=None, count=None, style='linear')
        commit_widget = SCMCommitWidget()
        related_artifacts_widget = RelatedArtifactsWidget()

    def __init__(self):
        self._discuss = AppDiscussionController()

    def _check_security(self):
        g.security.require_access(c.app, 'read')

    @expose()
    def refresh(self):
        repo_tasks.refresh.post()
        if request.referer:
            flash('Repository is being refreshed')
            redirect(request.referer)
        else:
            return '%r refresh queued.\n' % c.app.repo

    @expose(TEMPLATE_DIR + 'tree.html')
    @expose('json')
    def folder(self, rev, *args, **kw):
        c.commit, c.folder, rev = get_commit_and_obj(rev, *args)
        if c.folder.kind == 'File':
            redirect(
                c.commit.url_for_method('file') + '/' + '/'.join(args), **kw)

        # get cache, if available
        if g.cache:
            data = g.cache.hget_json(c.folder.cache_name, 'tree_json') or {}
        else:
            data = {}

        if not data:
            for entry in c.folder.ls(include_self=True):
                entry.setdefault('extra', {})
                entry['extra']['forkUrl'] = '{}_modify/fork_artifact'.format(
                    c.app.url)

                # no href because we calculate the url from the path, so that
                # we can stay with the current rev (which can be non-absolute)
                # e.g. branch names, head
                entry.pop("href", None)

                # File-specific info
                if entry["type"] == "FILE":
                    entry['extra']['size'] = h.pretty_print_file_size(
                        entry["size"])
                    visualizers = Visualizer.get_for_resource(
                        entry["path"], cache=True)
                    if len(visualizers) > 0:
                        entry['extra']['iconURL'] = visualizers[0].icon_url

                data[entry["path"]] = entry

            # set cache
            if g.cache:
                g.cache.hset_json(c.folder.cache_name, 'tree_json', data)

        return dict(rev=rev, data=data)

    @expose('json')
    def dir_last_commits(self, rev, *args, **kwargs):
        c.commit, c.folder, rev = get_commit_and_obj(rev, *args)
        data = {}

        # try to load cached data
        paths = None
        cache_result = False
        if g.cache:
            tree_data = g.cache.hget_json(c.folder.cache_name, 'tree_json')
            if tree_data:
                cache_result = True
                paths = []
                path_i = len(c.folder.path)
                for path, info in tree_data.iteritems():
                    if not info.get('extra', {}).get('commit'):
                        paths.append(path[path_i:])
                    else:
                        data[path] = {
                            'extra': {'commit': info['extra']['commit']}
                        }
                if not paths:  # we have all the info we need
                    return {'data': data}

        commit_info = c.folder.ls_commits(include_self=True, paths=paths)
        for path, last_commit in commit_info.iteritems():
            # commit text
            commit_text = ''
            if last_commit['href'] is not None:
                # generate avatar
                author_content = self.Widgets.commit_author_widget.display(
                    last_commit)
                commit_text = (
                    '{0} <a href="{href}">[{shortlink}]</a>{summary}').format(
                        author_content, **last_commit)

            data[path] = {
                'extra': {'commit': commit_text}
            }
            if cache_result:
                tree_data[path].setdefault('extra', {})
                tree_data[path]['extra'].setdefault('commit', {})
                tree_data[path]['extra']['commit'] = commit_text

        if cache_result:
            g.cache.hset_json(c.folder.cache_name, 'tree_json', tree_data)

        return {'data': data}

    @expose(TEMPLATE_DIR + 'readme.html')
    def readme(self, rev, *args, **kwargs):
        commit, folder, rev = get_commit_and_obj(rev, *args)
        readme_file = folder.readme()
        result = {'name': None}
        if readme_file:
            text = readme_file.open().read()
            if text:
                renderer = g.pypeline_markup.renderer(readme_file.name)
                if renderer[1]:
                    text = g.pypeline_markup.render(readme_file.name, text)
                result = {
                    'name': readme_file.name,
                    'text': text
                }
        return result

    @expose(TEMPLATE_DIR + 'file.html')
    def file(self, rev, *args, **kw):
        c.commit, c.file, rev = get_commit_and_obj(rev, *args, use_ext=True)
        if c.file.kind == 'Folder':
            redirect(
                c.commit.url_for_method('folder') + '/' + '/'.join(args), **kw)
        if kw.get('format') == 'raw':
            escape = asbool(kw.get('escape'))
            set_download_headers(c.file.name)
            if escape:
                return iter(cgi.escape(c.file.read()))
            return iter(c.file.open())
        else:
            c.related_artifacts_widget = self.Widgets.related_artifacts_widget
            c.visualizer_widget = self.Widgets.visualizer_widget
            c.thread = self.Widgets.thread_widget
            extra_params = kw.get('extra_params')
            if extra_params:
                extra_params = urllib.unquote(extra_params)
            return dict(
                thread=c.file.discussion_thread,
                force_display='force' in kw,
                extra_params=extra_params,
                rev=rev
            )

    @expose(TEMPLATE_DIR + 'diff.html')
    def diff(self, rev, *args, **kw):
        c.commit, c.file, rev = get_commit_and_obj(rev, *args, use_ext=True)
        a_ci = c.app.repo.commit(kw['diff'])
        if not a_ci:
            raise exc.HTTPNotFound()

        a = a_ci.get_path(c.file.path)
        if not a:
            raise exc.HTTPNotFound()

        c.diff_widget = self.Widgets.diff_widget
        return dict(a=a, b=c.file)

    @expose(TEMPLATE_DIR + 'commit.html')
    def commit(self, rev, *args, **kw):
        commit, rev, _ = get_commit(rev, args)
        c.related_artifacts_widget = self.Widgets.related_artifacts_widget
        c.commit_widget = self.Widgets.commit_widget
        result = {'commit': commit}
        result.update(commit.context())
        return result

    @expose(TEMPLATE_DIR + 'log.html')
    def history(self, rev, *args, **kw):
        c.commit, rev, _ = get_commit(rev, args)
        path = get_path(args, use_ext=True)
        if path == '/':
            path = None

        limit = int(kw.pop('limit', 10))
        if limit > 50:
            raise exc.HTTPBadRequest('limit must be < 50')
        page = int(kw.pop('page', 0))
        limit, page, start = g.handle_paging(limit, page)
        revisions = c.commit.log(start, limit, path=path)
        c.log_widget = self.Widgets.log_widget
        c.commit_author_widget = self.Widgets.commit_author_widget
        result = {
            'log': revisions,
            'path': path,
            'page': page,
            'limit': limit,
            'count': len(revisions),
            'rev': rev
        }
        result.update(kw)
        return result

    @with_trailing_slash
    @expose(TEMPLATE_DIR + 'fork.html')
    def fork(self, to_name=None, to_label=None, project_name=None):
        g.security.require_authenticated()
        if not c.app.forkable:
            raise exc.HTTPNotFound

        project_opts = []
        for project in c.user.my_projects():
            if project.is_real() and g.security.has_access(project, 'admin'):
                project_opts.append({
                    'shortname': project.shortname,
                    'name': project.name,
                    'selected': project.shortname == project_name
                })

        return dict(
            project_opts=project_opts,
            to_name=to_name or c.app.config.options.mount_point,
            to_label=to_label or c.app.config.options.mount_label
        )

    @expose()
    @require_post()
    def do_fork(self, to_name=None, to_label=None, project_name=None):
        g.security.require_authenticated()

        # collect params
        from_repo = c.app.repo
        from_project = c.project
        to_project = Project.query.get(shortname=project_name)
        to_name = to_name or c.app.config.options.mount_point
        to_label = to_label or c.app.config.options.mount_label

        # access control
        g.security.require_access(to_project, 'admin')

        with push_config(c, project=to_project):
            if not to_project.database_configured:
                to_project.configure_project(is_user_project=True)

            if c.project.app_config(to_name):
                flash(
                    'That Name is already taken by an existing tool', 'error')
                redirect('fork')

            try:
                to_project.install_app(
                    from_repo.tool_name, to_name, to_label,
                    cloned_from_project_id=from_project._id,
                    cloned_from_repo_id=from_repo._id)
            except Exception:
                flash('An unknown error occurred', 'error')
                LOG.exception('Error forking repository {} to {}/{}'.format(
                    from_repo, c.project.shortname, to_name
                ))
                redirect('fork')

        redirect(to_project.url() + to_name + '/')

    @without_trailing_slash
    @expose()
    @validate(dict(
        since=DateTimeConverter(if_empty=None, if_invalid=None),
        until=DateTimeConverter(if_empty=None, if_invalid=None),
        offset=validators.Int(if_empty=None),
        limit=validators.Int(if_empty=None)))
    def feed(self, since=None, until=None, offset=None, limit=None):
        if request.environ['PATH_INFO'].endswith('.atom'):
            feed_type = 'atom'
        else:
            feed_type = 'rss'
        title = 'Recent changes to %s' % c.app.config.options.mount_point
        feed = Feed.feed(
            dict(project_id=c.project._id, app_config_id=c.app.config._id),
            feed_type,
            title,
            c.app.url,
            title,
            since, until, offset, limit)
        response.headers['Content-Type'] = ''
        response.content_type = 'application/xml'
        return feed.writeString('utf-8')

    @without_trailing_slash
    @expose(TEMPLATE_DIR + 'commit_browser.html')
    def commit_browser(self):  # pragma no cover
        if True or not c.app.repo.status in ('ready', 'analyzing'):
            return dict(status='not_ready')

        count = c.app.repo.count()
        if not count:
            return dict(status='no_commits')
        c.commit_browser_widget = self.Widgets.commit_browser_widget
        all_commits = c.app.repo._impl.new_commits(all_commits=True)
        sorted_commits = dict()
        next_column = 0
        series = 0
        free_cols = set()
        for i, commit in enumerate(reversed(all_commits)):
            c_obj = Commit.query.get(object_id=commit)
            c_obj.repo = c.app.repo
            if commit not in sorted_commits:
                col = next_column
                if len(free_cols):
                    col = free_cols.pop()
                else:
                    next_column += 1
                sorted_commits[commit] = dict(column=col, series=series)
                series += 1
            sorted_commits[commit]['row'] = i
            sorted_commits[commit]['parents'] = []
            sorted_commits[commit]['message'] = c_obj.summary
            sorted_commits[commit]['url'] = c_obj.url()
            for j, parent in enumerate(c_obj.parent_ids):
                sorted_commits[commit]['parents'].append(parent)
                parent_mapped = parent in sorted_commits and\
                    sorted_commits[parent]['column'] > \
                    sorted_commits[commit]['column']
                if (parent not in sorted_commits or parent_mapped) and j == 0:
                    # this parent is the branch point for a different column,
                    # so make that column available for re-use
                    if parent_mapped:
                        free_cols.add(sorted_commits[parent]['column'])
                    sorted_commits[parent] = dict(
                        column=sorted_commits[commit]['column'],
                        series=sorted_commits[commit]['series']
                    )
                # this parent is the branch point for this column, so make this
                # column available for re-use
                elif parent in sorted_commits and\
                    sorted_commits[parent]['column'] <\
                        sorted_commits[commit]['column']:
                    free_cols.add(sorted_commits[commit]['column'])
        return dict(
            built_tree=json.dumps(sorted_commits),
            next_column=next_column,
            max_row=len(all_commits),
            status='ready')


class ModifyController(object):
    """
    Controller for modifying the contents of the repository through the web
    interface

    """

    @expose('json')
    def fork_artifact(self, branch=None, dir_path=None, artifact_ref=None):
        """
        Add exchange component to a folder. Unpacks the files associated with
        an exchange component, commits the changes, and returns the new url
        associated with dir_path.

        @param dir_path: str    path to directory to unpack component
                                (from repo root)
        @param artifact_ref: str    index_id of exchange component
        @return: str    url of new dir_path

        """
        g.security.require_access(c.app, 'write')

        # get component
        artifact_ref = urllib.unquote(artifact_ref)
        artifact = ArtifactReference.artifact_by_index_id(artifact_ref)
        if not artifact:
            raise exc.HTTPNotFound

        # enforce read permission on artifact
        if not artifact.app_config.is_visible_to(c.user):
            raise exc.HTTPForbidden, "Read Permission Denied on Artifact"

        # extract component files
        temp_dir = tempfile.mkdtemp()
        filename = artifact.get_content_to_folder(temp_dir)
        if filename is None:
            return dict(status="failure", exc="Resource files not found")
        new_file = os.path.join(temp_dir, filename)

        # update the repository
        c.app.repo.add_file(
            new_file,
            dir_path,
            'Add %s' % artifact.link_text_short(),
            branch=branch,
            author=c.user.display_name
        )
        try:
            os.remove(new_file)
        except (OSError, IOError):
            LOG.exception('Error cleaning forked artifact %s', new_file)

        # add a reference
        ci = c.app.repo.latest()
        obj = ci.tree.get_from_path(os.path.join(dir_path, filename))
        if obj:
            obj_ref = ArtifactReference.from_artifact(obj)
            obj_ref.upsert_reference(artifact_ref)

        parent = obj.parent

        return dict(
            status="success",
            url=parent.url(),
            commit_url=ci.url(),
        )


class RootRestController(BaseController):

    def __init__(self):
        super(BaseController, self).__init__()
        self.artifact = ArtifactRestController()
        self.alternate = RepoAlternateRestController()

    def _check_security(self):
        g.security.require_access(c.app, 'read')

    @expose()
    def file(self, rev, *args, **kw):
        ci, file, rev = get_commit_and_obj(rev, *args, use_ext=True)
        set_download_headers(file.name)
        return iter(file.open())


class RepoAlternateRestController(BaseAlternateRestController):

    @expose('json')
    def get_one(self, rev, *args, **kw):
        ci, self.artifact, rev = get_commit_and_obj(rev, *args, use_ext=True)
        return super(RepoAlternateRestController, self).get_one(**kw)

    @expose()
    def put(self, rev, *args, **kw):
        ci, self.artifact, rev = get_commit_and_obj(rev, *args, use_ext=True)
        response = super(RepoAlternateRestController, self).put(**kw)
        session(self.artifact.alt_object).flush()
        return response

    @expose('json')
    def post(self, rev, *args, **kw):
        """
        Queues a processesing operation to generate an alternative resource

        """
        context = kw.get('context', 'visualizer')
        ci, self.artifact, rev = get_commit_and_obj(rev, *args, use_ext=True)
        self._assert_can_process(context)
        repo_tasks.process_file.post(
            kw.get('processor'), context, rev, self.artifact.path)
        self.artifact.alt_loading = True
        return {
            'success': True
        }