"""
Basic model for repositories
"""
import cgi
import os
import errno
import logging
import string
import re
from datetime import datetime
from operator import itemgetter
from itertools import chain, izip_longest
from urlparse import urlparse

import tg
from pylons import tmpl_context as c, app_globals as g
import pymongo.errors
from ming import schema as S
from ming.utils import LazyProperty
from ming.odm import FieldProperty, RelationProperty, session, state
from ming.odm.property import ORMProperty, ManyToOneJoin

from vulcanforge.common import helpers as h
from vulcanforge.common.model.session import repository_orm_session
from vulcanforge.artifact.tasks import add_artifacts
from vulcanforge.common.util import ConfigProxy
from vulcanforge.artifact.model import (
    Artifact,
    Feed,
    ArtifactApiMixin,
    ArtifactReference,
    Shortlink
)
from vulcanforge.auth.model import User
from vulcanforge.common.util.filesystem import import_object
from vulcanforge.discussion.model import Thread
from vulcanforge.project.model import AppConfig, Project
from vulcanforge.notification.model import Notification
from vulcanforge.taskd import model_task
from vulcanforge.visualize.base import VisualizableMixIn

from vulcanrepo.exceptions import RepoNoJoin
from .hook import PostCommitHook

log = logging.getLogger(__name__)
config = ConfigProxy(
    common_suffix='forgemail.domain',
    common_prefix='forgemail.url')

README_RE = re.compile('^README(\.[^.]*)?$', re.IGNORECASE)


class RepositoryThread(Thread):
    """
    Discussion thread for repository files and folders.

    Note that the ref_id property is simply a path for this guy

    """

    class __mongometa__:
        polymorphic_identity = 'repo_thread'

    type_s = 'RepoThread'

    kind = FieldProperty(str, if_missing='repo_thread')
    ref = None


class RepositoryContent(ArtifactApiMixin):
    """Base class for repository files and folders. Contains properties and
    methods common to each.

    Repository content information is only persisted within the repository
    itself. This object and its subclasses provide a unified interface for
    interacting with the underlying files and folders.

    """
    kind = None  # File, Folder
    type_s = None  # For indexing, if necessary
    acl = []

    def __init__(self, commit, path):
        """
        :param commit Commit
        :param path str

        """
        self.commit = commit
        self.path = path

    @property
    def repo(self):
        """Get the associated Repository object"""
        return self.commit.repo

    def ls_entry(self, escape=False):
        """Metadata about this content object"""
        name = h.really_unicode(self.name)
        path = self.path
        if escape:
            name = cgi.escape(name)
            path = cgi.escape(path)
        date = self.commit.committed['date']
        return {
            "name": name,
            "path": path,
            "href": self.url(),
            "date": date.isoformat(),
            "type": "FILE" if self.kind == "File" else "DIR",
            "artifact": {
                'reference_id': self.index_id(),
                'type': self.type_s
            }
        }

    # artifact-ness
    @property
    def app_config_id(self):
        return self.repo.app_config_id

    @property
    def app_config(self):
        return self.repo.app_config

    def url(self):
        """Url of the object within the forge"""
        pass

    def link_text_short(self):
        return self.name if self.name else '/'

    def shorthand_id(self):
        return u'({}){}'.format(self.commit.shorthand_id(), self.path)

    def index_id(self):
        return u'Repo.{}.{}.{}'.format(
            self.app_config_id, self.commit.object_id, self.path)

    @property
    def cache_name(self):
        return '.'.join((
            str(self.app_config_id),
            self.version_id,  # mixins should apply this (do not add here)
            self.path
        ))

    def url_for_rev(self, rev):
        """Get the url of the object at the same path with specified revision
        string

        """
        raise NotImplementedError('url_for_rev')

    def url_for_method(self, method):
        """Url for a given controller method. Primarly used internally."""
        return self.commit.url_for_method(method) + h.urlquote(self.path)


class RepositoryFile(RepositoryContent, VisualizableMixIn):
    """Facade for interacting with a repository file"""
    kind = 'File'
    visualizable_kind = 'repository_file'
    type_s = 'Blob'
    link_type = 'file'
    folder_cls = None

    @classmethod
    def find_for_task(cls, commit_cls_path, commit_id, path):
        """Used in conjunction with `get_task_lookup_args` to allow for the
        retrieval of RepositoryFile instances when performing asynchronous
        tasks.

        """
        commit_cls = import_object(commit_cls_path)
        ci = commit_cls.query.get(_id=commit_id)
        return ci.get_path(path)

    @property
    def name(self):
        """Returns the filename"""
        return os.path.basename(self.path)

    @property
    def size(self):
        """Size of the file, in bytes"""
        raise NotImplementedError('size')

    @LazyProperty
    def parent(self):
        """Get the containing folder"""
        parent_path = os.path.dirname(self.path)
        return self.folder_cls(self.commit, parent_path)

    def url(self):
        """Url to visualize the file in the forge, if possible"""
        return self.url_for_method('file')

    def url_for_rev(self, rev):
        return self.repo.url() + 'file/' + rev + h.urlquote(self.path)

    def raw_url(self):
        """Url to download the file"""
        return self.url() + '?format=raw'

    def open(self):
        """Open as a file pointer"""
        raise NotImplementedError('open')

    def read(self):
        """Get raw content as a string"""
        raise NotImplementedError('read')

    def ls_entry(self, escape=False):
        entry = super(RepositoryFile, self).ls_entry(escape=escape)
        entry.update({
            "downloadURL": self.raw_url(),
            'size': self.size
        })
        return entry

    def get_content_hash(self):
        """Return a hash of the content. Files with identical content should
        have identical hashes.

        """
        raise NotImplementedError('get_content_hash')

    def get_discussion_thread(self, data=None, generate_if_missing=True):
        t = RepositoryThread.query.get(ref_id=self.path)
        if t is None and generate_if_missing:
            t = RepositoryThread(
                discussion_id=self.app_config.discussion_id,
                ref_id=self.path,
                subject='%s discussion' % self.path)
            session(RepositoryThread).flush(t)
        return t

    def get_content_to_folder(self, path, **kw):
        """Download the content to a local folder at :path"""
        full_path = os.path.join(path, self.name)
        with open(full_path, 'w') as fp:
            src = self.open()
            fp.write(src.read())
            src.close()
        return self.name

    def get_unique_id(self):
        """Unique identifier of this version of this file.

        This should be the same for a given file across multiple commits, so
        long as the file is not modified in any of the commits, and different
        across commits in which the file was modified.

        """
        return 'RepoVersion' + '.'.join(
            (str(self.app_config_id), self.path, self.version_id))

    def artifact_ref_id(self):
        """Used for the visualizer infrastructure"""
        return self.index_id()

    def get_task_lookup_args(self):
        """Used in conjunction with `find_for_task` to allow for the
        retrieval of RepositoryFile instances when performing asynchronous
        tasks.

        """
        commit_cls_path = '{}:{}'.format(self.commit.__class__.__module__,
                                         self.commit.__class__.__name__)
        return [commit_cls_path, self.commit._id, self.path]


class RepositoryFolder(RepositoryContent):
    """Facade for interacting with a repository folder"""
    kind = 'Folder'
    type_s = 'Tree'

    def __init__(self, commit, path):
        if not path.endswith('/'):
            path += '/'
        self.name = path.rsplit('/', 2)[-2]
        super(RepositoryFolder, self).__init__(commit, path)

    @LazyProperty
    def parent(self):
        """Get the parent folder of this folder, if any"""
        if self.path != '/':
            parent_path = os.path.normpath(os.path.join(self.path, os.pardir))
            return self.__class__(self.commit, parent_path)

    def url_for_rev(self, rev):
        return self.repo.url() + 'folder/' + rev + self.path

    def url(self):
        return self.url_for_method('folder')

    def __getitem__(self, item):
        """Retrieve relative path"""
        return self.commit.get_path(self.path + item)

    def __iter__(self):
        """Implement in subclass. Should iterate through all files and folders
        contained in this folder. Does not descend into subfolders.

        """
        raise NotImplementedError('__iter__')

    def walk(self, ignore=[]):
        """Generator that yields files and folders in this folder or any
        subfolder.

        If :param ignore is provided, files or folders of this name are not
        returned.

        """
        folders = [self]
        while folders:
            _new_folders = []
            for obj in chain(*folders):
                if obj.name not in ignore:
                    yield obj
                    if obj.kind == 'Folder':
                        _new_folders.append(obj)
            folders = _new_folders

    def find_files(self):
        """Generator that yields all RepositoryFiles in this folder or any
        subfolder.

        """
        for obj in self.walk():
            if obj.kind == 'File':
                yield obj

    def ls(self, include_self=False, escape=False):
        objs = chain([self], iter(self)) if include_self else iter(self)
        return [obj.ls_entry(escape=escape) for obj in objs]

    def get_from_path(self, path):
        """Get file or folder using relative path. OS-style """
        full_path = os.path.normpath(os.path.join(self.path, path))
        return self.commit.get_path(full_path)

    def readme(self):
        """Find a readme in this folder. If none found, returns None."""
        for obj in self:
            if obj.kind == 'File' and README_RE.match(obj.name):
                return obj
        return None

    def get_content_to_folder(self, path, ignore=[], contents_only=False):
        """Download this folder and all of its contents to a local filesystem
        folder

        """
        if not contents_only and self.name:
            path = os.path.join(path, self.name)
        if not os.path.exists(path):
            os.makedirs(path)
        for obj in self:
            if not obj.name in ignore:
                obj.get_content_to_folder(path, ignore=ignore)
        return self.name


class Repository(Artifact):
    """Database Representation of a Repository"""
    BATCH_SIZE = 100
    post_receive_template = string.Template('#!/bin/bash\ncurl -s -k $url\n')
    commit_cls = None

    class __mongometa__:
        name = 'generic-repository'

    repo_id = 'repo'
    type_s = 'Repository'

    name = FieldProperty(str)
    tool = FieldProperty(str)
    fs_path = FieldProperty(str)
    url_path = FieldProperty(str)
    status = FieldProperty(str)
    additional_viewable_extensions = FieldProperty(str)
    upstream_repo = FieldProperty(dict(name=str, url=str))
    post_commit_hooks = FieldProperty([S.Object(dict(
        plugin_id=S.ObjectId,
        args=[None],
        kwargs=None
    ))])

    def __init__(self, **kw):
        log.info("Repository init. keyword arguments: %s" % kw)
        if 'name' in kw and 'tool' in kw:
            if 'fs_path' not in kw:
                kw['fs_path'] = self.default_fs_path(c.project, kw['tool'])
            if 'url_path' not in kw:
                kw['url_path'] = self.default_url_path(c.project, kw['tool'])
        super(Repository, self).__init__(**kw)

    def __repr__(self):
        return '<%s %s>' % (self.__class__.__name__, self.full_fs_path)

    @classmethod
    def default_fs_path(cls, project, tool):
        repos_root = tg.config.get('scm.repos.root', '/')
        dirname = project.shortname
        if dirname == '--init--':
            dirname = project.neighborhood.url_prefix.strip('/')
        path = os.path.join(
            repos_root, tool, tg.config.get('scm.fs_prefix', 'projects'),
            dirname
        )
        return path + '/'

    @classmethod
    def default_url_path(cls, project, tool):
        dirname = project.shortname
        if dirname == '--init--':
            dirname = project.neighborhood.url_prefix.strip('/')
        path = os.path.join(
            tg.config.get('scm.fs_prefix', 'projects'), dirname)
        return '/' + path + '/'

    @property
    def url_name(self):
        return self.name

    @property
    def full_fs_path(self):
        """Path to the repository on the filesystem"""
        return os.path.join(self.fs_path, self.name)

    @property
    def supports_http(self):
        domain = tg.config.get(
            'scm.domain.{}'.format(self.repo_id),
            tg.config.get('scm.domain', 'localhost')
        )
        base_domain = urlparse(g.base_url).hostname
        return domain != base_domain

    @property
    def email_address(self):
        """Used in notifications"""
        domain = '.'.join(
            reversed(self.app.url[1:-1].split('/'))).replace('_', '-')
        return 'noreply@%s%s' % (domain, config.common_suffix)

    @property
    def url_map(self):
        """Dictionary with keys ro, rw, https, https_anon specifying source
        urls for checkout purposes

        """
        raise NotImplementedError('url_map')

    def _setup_paths(self, create_repo_dir=True):
        """Upsert the path to the repository"""
        fullname = self.full_fs_path
        path = fullname if create_repo_dir else self.fs_path
        try:
            os.makedirs(path)
        except OSError as e:
            if e.errno != errno.EEXIST:  # pragma no cover
                raise
            else:
                log.warn('setup_paths error %s' % path, exc_info=True)
        return fullname

    def _setup_hooks(self):  # pragma no cover
        """Install a hook in the repository that will ping the refresh url for
        the repo.

        Implement in subclass.

        """
        pass

    def init_as_clone(self, source_name, source_url):
        """Initialize as clone of another repository"""
        self.upstream_repo.name = source_name
        self.upstream_repo.url = source_url
        session(self.__class__).flush(self)
        self.clone_from(source_url)

    def clone_from(self, source_url):
        """Clone from a given source repository. Implement in subclass"""
        raise NotImplementedError('clone_from')

    def refresh_heads(self):
        """Store metadata about current state of the repo, including latest
        commit.

        """
        raise NotImplementedError('refresh_heads')

    def refresh_commit(self, ci):
        """Store author, log, etc on commit object by analyzing underlying
        repository representation.

        """
        raise NotImplementedError('refresh_commit')

    def new_commits(self, all_commits=False):
        """Find any new commit ids that have not been analyzed by the forge. If
        all_commits is True, return all commit ids.

        :param all_commits bool
        """
        raise NotImplementedError('new_commits')

    def commit(self, rev=None):
        """Get a commit by revision string|int."""
        raise NotImplementedError('commit')

    def url(self):
        return self.app_config.url()

    def shorthand_id(self):
        return self.name

    def index(self, **kw):
        return super(Repository, self).index(
            name_s=self.name,
            type_s=self.type_s,
            title_s='Repository %s %s' % (self.project.name, self.name),
            text_objects=[self.name],
            **kw
        )

    def suggested_clone_dest_path(self):
        """Suggested mount point for new repository"""
        if c.project.shortname == '--init--':
            prefix = c.project.neighborhood.url_prefix.strip('/')
        else:
            prefix = c.project.shortname.replace('/', '-')
        return '{}-{}'.format(prefix, self.url_name)

    def clone_url(self, category, username=''):
        """
        Return a URL string suitable for copy/paste that describes _this_ repo,
        e.g., for use in a clone/checkout command

        """
        if not username and c.user and not c.user.is_anonymous:
            username = c.user.username
        scheme_map = {
            'ro': 'http',
            'rw': 'ssh',
            'https': 'https',
            'https_anon': 'https'
        }
        domain = tg.config.get(
            'scm.domain.{}'.format(self.repo_id),
            tg.config.get('scm.domain', 'localhost')
        )
        port_defaults = {
            'http': "80",
            'ssh': "22",
            'https': "443"
        }
        scheme = scheme_map[category]
        port = tg.config.get(
            'scm.port.{}.{}'.format(scheme, self.repo_id),
            tg.config.get('scm.port.{}'.format(scheme), port_defaults[scheme])
        )
        return self.url_map[category].format(
            host='{}'.format(domain, port),
            domain=domain,
            port=port,
            path=self.url_path + self.url_name,
            username=username
        )

    def clone_command(self, category, username=''):
        """
        Return a string suitable for copy/paste that would clone this repo
        locally category is one of 'ro' (read-only), 'rw' (read/write), or
        'https' (read/write via https)

        """
        raise NotImplementedError('clone_command')

    def upsert_post_commit_hook(self, pch, args=None, kwargs=None):
        """Install the given PostCommitHook instance to this repository.

        If args and/or kwargs are specified, the object the hook refers to
        will be instantiated with the given args/kwargs.

        """
        if args is None:
            args = []
        if kwargs is None:
            kwargs = {}
        for p in self.post_commit_hooks:
            if p.plugin_id == pch._id:
                p.update({"args": args, "kwargs": kwargs})
                return False
        self.post_commit_hooks.append({
            'plugin_id': pch._id,
            'args': args,
            'kwargs': kwargs})
        return True

    def remove_post_commit_hook(self, plugin_id):
        """
        Uninstall the post commit hook with the given id from this repository

        """
        for i, p in enumerate(self.post_commit_hooks):
            if p.plugin_id == plugin_id:
                break
        else:
            return False
        del self.post_commit_hooks[i]
        return True

    @model_task
    def run_post_commit_hooks(self, commit_ids):
        """
        Run post commit hooks on a sequence of commits

        @param commit_ids: sequence of Commit object ids
        @return: None

        """
        commits = [
            self.commit_cls.query.get(repository_id=self._id, object_id=oid)
            for oid in commit_ids]
        for hook, args, kwargs in self.get_hooks():
            log.info('Running Postcommit hook %s on %d commits' % (
                hook.shortname, len(commits)))
            hook.run(commits, args=args, kwargs=kwargs)
            log.info('Hook complete')

    @model_task
    def run_batched_post_commit_hooks(self, commit_ids=None):
        """
        Run post commit hooks in batches so as not to exceed available memory

        @param commit_ids: iterable of oids, default is all commits
        @return: None

        """
        if commit_ids is None:
            commit_ids = self.new_commits(True)
        log.info('Running Batch Commit Hooks on %d commits', len(commit_ids))
        cid_iter = izip_longest(*[iter(commit_ids)] * self.BATCH_SIZE)
        for batch_commit_ids in cid_iter:
            self.run_post_commit_hooks(
                filter(lambda x: x is not None, batch_commit_ids))
        log.info('Post Commit Hooks complete')

    def get_hooks(self):
        """
        Generator that yields:
        (PostCommitHook instance, args list, kwargs dict)

        """
        for pch in self.post_commit_hooks:
            hook = PostCommitHook.query.get(_id=pch.plugin_id)
            if hook is not None:
                yield hook, pch.get('args'), pch.get('kwargs')
            else:
                log.warn("No Postcommit hook found with id %s" % pch.plugin_id)

    def post_commit_feed(self, ci):
        """Store a feed instance for this commit"""
        return Feed.post(
            self,
            title='New commit',
            description='%s<br><a href="%s%s">View Changes</a>' % (
                ci.summary, config.common_prefix, ci.url()),
            author_link=ci.user.url() if ci.user else None,
            author_name=ci.user.display_name if ci.user else None
        )

    def notify_commits(self, commit_msgs, last_commit=None):
        """Create notification(s) for this repository given a list of commit
        messages.

        """
        if len(commit_msgs) > 1:
            subject = '%d new commits to %s %s' % (
                len(commit_msgs),
                self.app.project.name,
                self.app.config.options.mount_label)
        elif last_commit:
            subject = '%s committed to %s %s: %s' % (
                last_commit.committed.name,
                self.app.project.name,
                self.app.config.options.mount_label,
                last_commit.summary)
        else:  # pragma no cover
            subject = 'New commit(s) to {} {}'.format(
                self.app.project.name,
            )
        text = '\n\n'.join(commit_msgs)
        # pass last committer to notification as user
        author = last_commit.user if last_commit else None
        notification = Notification.post(
            artifact=self,
            topic='metadata',
            subject=subject,
            text=text,
            author=author
        )
        return notification

    def refresh(self, all_commits=False, notify=True, with_hooks=True,
                update_status=True):
        """Find any new commits in the repository and update"""
        self.refresh_heads()  # updates repository metadata
        if update_status:
            self.status = 'analyzing'
            session(self.__class__).flush()

        commit_ids = self.new_commits(all_commits)
        log.info('Refreshing %d new commits in %s', len(commit_ids), self)

        sess = session(self.commit_cls)
        commit_msgs = []
        ref_ids = []
        new_commit_ids = []
        lc = None

        # Add commit objects to the db
        for i, oid in enumerate(commit_ids):
            ci, isnew = self.commit_cls.upsert(oid, self._id)
            # race condition if not all_commits
            if not isnew and not all_commits:
                sess.expunge(ci)
                continue

            # refresh and create metadata
            ci.set_context(self)
            self.refresh_commit(ci)
            ArtifactReference.from_artifact(ci)
            Shortlink.from_artifact(ci)
            ref_ids.append(ci.index_id())
            lc = ci

            # periodic flushing for large commit collections
            if (i + 1) % self.BATCH_SIZE == 0:
                sess.flush()
                sess.clear()

            # Collect Notifications
            if notify:
                self.post_commit_feed(ci)
                commit_msgs.append(ci.notification_message)

            new_commit_ids.append(oid)

        # Send Notifications
        if notify and commit_msgs:
            self.notify_commits(commit_msgs, last_commit=lc)

        sess.flush()
        sess.clear()

        # Index the commits
        if ref_ids:
            add_artifacts(ref_ids, update_solr=False)

        log.info('Refreshed repository %s.', self)
        if update_status:
            self.status = 'ready'
            session(self.__class__).flush()

        # Run Pluggable Post Commit Hooks
        if with_hooks:
            if all_commits:
                self.run_batched_post_commit_hooks.post()
            else:
                # do individual queries to maintain order
                self.run_post_commit_hooks.post(new_commit_ids)

        return len(commit_ids)

    def push_upstream_context(self):
        """Enter context of upstream repository"""
        project, rest = Project.by_url_path(self.upstream_repo.url)
        with g.context_manager.push(project._id):
            app = project.app_instance(rest[0])
        return g.context_manager.push(app_config_id=app.config._id)


class Commit(Artifact):
    """
    Metadata about a commit persisted for ease of search and retrieval.
    """

    class __mongometa__:
        session = repository_orm_session
        name = 'repo_commit'
        unique_indexes = [('object_id', 'repository_id')]

    type_s = 'Commit'

    _id = FieldProperty(S.ObjectId)
    object_id = FieldProperty(str)
    repository_id = FieldProperty(S.ObjectId)

    # File data
    diffs = FieldProperty(dict(
        added=[str],
        removed=[str],
        changed=[str],
        copied=[dict(old=str, new=str)]))
    # Commit metadata
    authored = FieldProperty(dict(
        name=str,
        email=str,
        date=datetime))
    message = FieldProperty(str)

    tool_version = FieldProperty({str: str}, if_missing={'repo': '1'})

    def __repr__(self):
        return '<%s %s>' % (self.__class__.__name__, self.object_id)

    @classmethod
    def new_by_object_id(cls, object_id, repository_id):
        return cls(object_id=object_id, repository_id=repository_id)

    @classmethod
    def upsert(cls, object_id, repository_id):
        isnew = False
        r = cls.query.get(object_id=object_id, repository_id=repository_id)
        if r is not None:
            return r, isnew
        try:
            r = cls.new_by_object_id(object_id, repository_id)
            session(cls).flush(r)
            isnew = True
        except pymongo.errors.DuplicateKeyError:  # pragma no cover
            session(cls).expunge(r)
            r = cls.query.get(object_id=object_id, repository_id=repository_id)
        return r, isnew

    @LazyProperty
    def repo(self):
        raise NotImplementedError('repo property')

    @property
    def app_config(self):
        return self.repo.app_config

    @LazyProperty
    def user(self):
        if self.authored.email:
            return User.by_email_address(self.authored.email)

    @property
    def url_rev(self):
        return self.object_id

    @property
    def committed(self):
        return self.authored

    @property
    def diffs_computed(self):
        if self.diffs.added:
            return True
        if self.diffs.removed:
            return True
        if self.diffs.changed:
            return True
        if self.diffs.copied:
            return True

    @property
    def paths_added(self):
        """Deprecated. This is only needed for backwards compat."""
        return set(
            self.diffs.added + map(itemgetter('new'), self.diffs.copied))

    @property
    def files_added(self):
        """Returns RepositoryFile instances for all files added in this commit

        """
        added_paths = set()
        added = []

        def add_path(added_path):
            if added_path not in added_paths:
                added_paths.add(added_path)
                added_obj = self.get_path(added_path, verify=False)
                if added_obj.kind == 'File':
                    added.append(added_obj)
                else:
                    for child in added_obj.find_files():
                        add_path(child.path)

        for path in self.paths_added:
            add_path(path)

        return added

    @property
    def files_modified(self):
        """Returns RepositoryFile instances for all files modified in this
        commit.

        """
        return [self.get_path(p, verify=False) for p in self.diffs.changed]

    @LazyProperty
    def summary(self):
        """Returns the first line of the log message truncated to 50 chars"""
        message = h.really_unicode(self.message)
        first_line = message.split('\n')[0]
        return h.truncate(first_line, 50)

    @property
    def notification_message(self):
        return u'<a href="{}{}" style="text-decoration: none">{}</a> by {}'.format(
            config.common_prefix,
            self.url(),
            h.really_unicode(self.summary),
            h.really_unicode(self.committed.name)
        )

    def get_path(self, path, verify=True):
        """
        Get the `RepositoryContent` instance at the given path.

        :param verify: bool. if False, it will not verify that the file/folder
        exists. Obviously, you should only do this if you're confident that it
        does exist.

        """
        raise NotImplementedError("get_path")

    def url(self):
        return self.url_for_method('commit')

    def url_for_method(self, method):
        return self.repo.url() + method + '/' + self.url_rev

    def index(self, **kwargs):  # pragma no cover
        return None

    def index_id(self):
        return '%s/%s#%s/%s' % (
            self.__class__.__module__.replace('.', '/'),
            self.__class__.__name__,
            str(self.repo._id),
            self.object_id
        )

    def get_link_content(self):
        """Content parsed for shortlinks to determine artifact relations"""
        return self.message

    def ref_category(self):
        return u"Commits"

    def set_context(self, repo):
        self.repo = repo

    def info(self):
        return {
            "date": self.authored.date,
            "author_name": self.authored.name,
            "author_email": self.authored.email,
            "id": self.object_id,
            "href": self.url(),
            "shortlink": self.shorthand_id(),
            "summary": self.summary
        }


#### MING ORM Properties for Repo Relations ###
class RepoBaseSpec(FieldProperty):
    """
    Base class for specifying the necessary information for relating to
    repo file artifacts.

    Analagous to ForeignIdProperty in standard Mingian
    relations

    """
    field_type = None

    def __init__(self, *args, **kwargs):
        super(RepoBaseSpec, self).__init__(self.field_type, *args, **kwargs)

    def set_from_obj(self, instance, obj):
        raise NotImplementedError('set_from_obj')

    def __set__(self, instance, value):
        if isinstance(value, RepositoryContent):
            self.set_from_obj(instance, value)
        super(RepoBaseSpec, self).__set__(instance, value)

    def get_obj(self, instance, cls=None):
        spec = self.__get__(instance, cls)
        app = None
        if getattr(c, 'app', None) and spec.app_config_id == c.app.config._id:
            app = c.app
        else:
            ac = AppConfig.query.get(_id=spec.app_config_id)
            if ac:
                App = ac.load()
                app = App(ac.project, ac)
        if app and getattr(app, 'repo', None):
            ci = app.repo.commit(spec.rev)
            if ci:
                return ci.get_path(spec.path)


class RepoCommitPathSpec(RepoBaseSpec):
    """For relating to a file/folder with commit/path"""
    field_type = S.Object({
        'app_config_id': S.ObjectId,
        'rev': str,
        'path': str
    })

    def set_from_obj(self, instance, obj):
        """set from a file or folder obj"""
        value = {
            'app_config_id': obj.repo.app_config_id,
            'rev': obj.commit.object_id,
            'path': obj.path
        }
        super(RepoCommitPathSpec, self).__set__(instance, value)


class RepoVersionSpec(RepoBaseSpec):
    """
    For relating to a unique version of a file. Similar to RepoCommitPathSpec
    except that it stores the "version_id" property of files/folders, which,
    combined with an app_config_id and path, uniquely identifies a file/folder
    version.

    This works a bit differently in svn vs. git due to speed issues and because
    the differences did not matter for the perceived use cases at the time
    of writing this.

    Both use the version_id property of their content objects to identify the
    file/folder version. In SVN, it is the last commit number. In Git, it is
    a hash of the file contents (using the hexsha property of files/folders
    stored by git). The effective difference is that in Git, if a file is
    modified in a commit, then reverted to its original state in a later
    commit, it will have the same version_id in its final form as in its
    original form, while in svn all versions will be unique.

    This, like RepoCommitPathSpec, can be combined with the RepoContentRelation
    property to retrieve a file/folder object. It will return the object
    at the commit originally used to set this property.

    To query the other way (retrieve mapped class instances given a file/folder
    object), search on the app_config_id, path, and version_id subproperties of
    this property.

    """
    field_type = S.Object({
        'app_config_id': S.ObjectId(if_missing=lambda: c.app.config._id),
        'rev': str,
        'path': str,
        'version_id': str
    })

    def set_from_obj(self, instance, obj):
        """set from a file or folder obj"""
        value = {
            'app_config_id': obj.repo.app_config_id,
            'rev': obj.commit.object_id,
            'path': obj.path,
            'version_id': obj.version_id
        }
        super(RepoVersionSpec, self).__set__(instance, value)


class RepoContentRelation(RelationProperty):
    """
    Retrieves repo file/folder using a repo spec

    Analagous to RelationProperty in Standard Ming Relations

    """
    def __init__(self, via=None, fetch=True):
        ORMProperty.__init__(self)
        self.via = via
        self.fetch = fetch

    def get_spec_property(self):
        if self.via:
            condition = lambda p: p.name == self.via
        else:
            condition = lambda p: isinstance(p, RepoBaseSpec)
        for p in self.mapper.all_properties():
            if condition(p):
                prop = p
                break
        else:
            raise RepoNoJoin('Cannot find repo spec property for {}'.format(
                self.mapper.mapped_class))
        return prop

    def __set__(self, instance, value):
        super(RepoContentRelation, self).__set__(instance, value)
        if self.fetch:
            st = state(instance)
            st.extra_state[self] = value

    @LazyProperty
    def join(self):
        prop = self.get_spec_property()
        return _RepoContentJoin(self.mapper.mapped_class, prop)


class _RepoContentJoin(ManyToOneJoin):

    def __init__(self, own_cls, prop):
        self.own_cls = own_cls
        self.prop = prop
        super(ManyToOneJoin, self).__init__()

    def load(self, instance):
        return self.prop.get_obj(instance, self.own_cls)

    def set(self, instance, value):
        self.prop.set_from_obj(instance, value)
