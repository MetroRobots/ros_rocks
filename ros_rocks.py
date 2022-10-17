import bs4
import click
import collections
import datetime
import dateutil.parser
import itertools
import re
import requests
from tqdm import tqdm
import yaml

ROS1 = ['indigo', 'jade', 'kinetic', 'lunar', 'melodic', 'noetic']
GITHUB_PATTERN = re.compile(r'^(https://github.com/[^/]+/([^/]+))(?:/|$)')
GITLAB_PATTERN1 = re.compile(r'^(https://gitlab.com.*/([^/\.]+))(?:\.git)?')
GITLAB_PATTERN2 = re.compile(r'^(https://gitlab[^/]+/.*/([^/\.]+))(?:\.git)?')
BB_PATTERN = re.compile(r'^(https://bitbucket.org.*/([^/\.]+))(?:\.git)?')
GC_PATTERN = re.compile(r'^(https://([^\.]+).googlecode.com)/svn')
REPO_PATTERNS = [GITHUB_PATTERN, GITLAB_PATTERN1, GITLAB_PATTERN2, BB_PATTERN, GC_PATTERN]


def distro_sorter(repo_name):
    if repo_name in ROS1:
        return (1, repo_name)
    else:
        return (2, repo_name)


def get_status_yamls():
    yaml_dict = {}
    for root in ['http://repositories.ros.org/status_page/yaml/', 'http://repo.ros2.org/status_page/yaml/']:
        req = requests.get(root)
        page = bs4.BeautifulSoup(req.content, 'html.parser')
        listing = page.find('pre')
        link = None
        for element in listing.children:
            if isinstance(element, bs4.element.Tag):
                href = element['href']
                if href == '../':
                    continue
                link = href
            elif link:
                elements = list(filter(None, map(str.strip, element.text.split(' '))))
                dt = dateutil.parser.parse(elements[0] + ' ' + elements[1])
                yaml_dict[link] = root + link, dt

    return yaml_dict


def download_yamls(cache_folder, debug=True):
    now = datetime.datetime.now()
    for filename, (full_url, dt) in get_status_yamls().items():
        target_path = cache_folder / filename
        if target_path.exists() and now - dt > datetime.timedelta(days=2):
            continue
        if debug:
            click.secho(f'Downloading {filename}...', fg='cyan')
        req = requests.get(full_url)
        with open(target_path, 'w') as f:
            f.write(req.content.decode())


def get_repo(url):
    for pattern in REPO_PATTERNS:
        m = pattern.match(url)
        if m:
            return {'url': m.group(1), 'name': m.group(2)}


def merge_dicts(a, b, path=[]):
    for k, v in b.items():
        if k not in a:
            a[k] = v
        elif isinstance(v, dict):
            merge_dicts(a[k], v, path + [k])
        elif a[k] == v:
            pass
        else:
            s = '/'.join(path)
            click.secho(f'Conflicting values {a[k]} {v} {s}', fg='red')


def get_distro_dicts(cache_folder, interactive=True):
    distro_paths = collections.defaultdict(dict)
    num_yamls = 0
    for path in cache_folder.glob('*yaml'):
        _, distro, name = path.stem.split('_')
        distro_paths[distro][name] = path
        num_yamls += 1

    if interactive:
        progress = tqdm(total=num_yamls)

    pkg_dict = collections.defaultdict(dict)
    for distro in sorted(distro_paths, key=distro_sorter):
        for name, path in distro_paths[distro].items():
            if interactive:
                progress.set_description(path.stem)
                progress.update()
            D = yaml.safe_load(open(path))
            for pkg, data in D.items():
                # rename `source` build
                for code_name_dict in data['build_status'].values():
                    for arch_dict in code_name_dict.values():
                        if 'source' in arch_dict:
                            arch_dict[f'{name}_source'] = arch_dict.pop('source')

                p_info = pkg_dict[pkg]

                # Merge maintainers
                if 'maintainers' not in p_info:
                    p_info['maintainers'] = data['maintainers']
                else:
                    for maintainer in data['maintainers']:
                        if maintainer not in p_info['maintainers']:
                            p_info['maintainers'].append(maintainer)

                repo = get_repo(data.get('url'))
                if repo:
                    if 'repo' not in p_info:
                        p_info['repo'] = []
                    if repo not in p_info['repo']:
                        p_info['repo'].append(repo)

                if 'build_status' not in p_info:
                    p_info['build_status'] = {}
                if distro not in p_info['build_status']:
                    p_info['build_status'][distro] = {}

                merge_dicts(p_info['build_status'][distro], data['build_status'])

    if interactive:
        progress.close()
    clean_dict = {}
    for k, v in pkg_dict.items():
        clean_dict[k] = dict(v)
    return clean_dict


def get_version_mapping(info):
    version_mapping = collections.defaultdict(list)
    for os_name, os_info in info.items():
        for os_code_name, code_info in os_info.items():
            for arch, arch_info in code_info.items():
                for release_build in ['build', 'main', 'test']:
                    long_version = arch_info.get(release_build)
                    if long_version:
                        version = long_version.split('-')[0]
                    else:
                        version = long_version
                    version_mapping[version].append({'os_name': os_name,
                                                     'os_code_name': os_code_name,
                                                     'arch': arch,
                                                     'release_build': release_build,
                                                     'is_source': 'source' if 'source' in arch else 'binary'})
    return dict(version_mapping)


def can_split_versions_with_combo(pkg_status, combo):
    """
    Group packages statuses by keys specified in combo.
    Determine if the different versions of the package can be evenly split
    across different values of the keys.
    For instance, if all the builds on main are version 0.1
    and all the versions on build/test are 0.2,
    that's an even split, so if the combo was just ['repo'], it would return
    {0.1: [main], 0.2: [build/test]}
    """
    version_lookup = {}
    version_mapping = collections.defaultdict(set)
    for version, keys in pkg_status.items():
        if version is None:
            version = 'missing'
        for key in keys:
            new_key = tuple([key[c] for c in combo])
            if new_key in version_lookup:
                if version_lookup[new_key] != version:
                    return None
            else:
                version_lookup[new_key] = version
            version_mapping[version].add('/'.join(new_key))
    return version_mapping


def find_optimal_split(pkg_status):
    keys = ['release_build', 'is_source', 'os_name', 'os_code_name', 'arch']
    for num_keys in range(1, len(keys) + 1):
        for combo in itertools.combinations(keys, num_keys):
            split_d = can_split_versions_with_combo(pkg_status, combo)
            if split_d:
                # Shortcut for most common case
                if combo == (keys[0],) and len(split_d) == 1:
                    only_version = list(split_d.keys())[0]
                    return None, {only_version: 'all'}

                # Format for caching
                ret_d = {}
                for k, v in split_d.items():
                    ret_d[k] = list(v)
                return combo, ret_d
    return None, {}


def tuple_version(s):
    if s == 'missing':
        return (0, 0, 0)
    pieces = s.split('.')
    return tuple(map(int, pieces))


def describe(split_combo, version_map, pkg):
    if not version_map:
        return {'class': 'bad', 'status': 'completely missing', 'version': 'x.x.x'}

    versions = sorted(version_map.keys(), key=tuple_version)

    # If there's only one version across all builds (and nothing is missing), then
    # this package is completely synced and released
    if len(version_map) == 1:
        return {'class': 'good', 'status': 'released', 'version': versions[0]}

    if split_combo == ('release_build',):
        if 'missing' in version_map:
            return {'class': 'new', 'status': 'waiting for new release', 'version': versions[-1]}
        else:
            return {'class': 'rerelease', 'status': 'waiting for re-release', 'version': versions[-1]}

    total_values = sum(len(v) for v in version_map.values())

    pct = len(version_map[versions[-1]]) / total_values
    if pct >= 0.75 or (len(version_map) == 2 and len(version_map[versions[0]]) == 1):
        old_vs = []
        for v in versions[:-1]:
            old_vs.append('{} ({})'.format(v, ', '.join(version_map[v])))
        return {'class': 'multiple', 'status': 'Old versions: ' + ('\n'.join(old_vs)), 'version': versions[-1]}
    elif total_values <= 2:
        old_vs = []
        for v in versions:
            old_vs.append('{} ({})'.format(v, ', '.join(version_map[v])))
        return {'class': 'multiple', 'status': '\n'.join(old_vs), 'version': versions[-1]}

    old_vs = []
    for v in versions:
        old_vs.append('{} ({})'.format(v, ', '.join(version_map[v])))

    return {'class': 'complicated', 'status': '\n'.join(old_vs), 'version': versions[-1]}


def get_status(distro_dict):
    status_dict = {}
    for pkg, pkg_dict in distro_dict.items():
        status_dict[pkg] = {'maintainers': pkg_dict['maintainers'], 'repo': pkg_dict['repo'], 'status': {}}
        for distro, pkg_info in pkg_dict['build_status'].items():
            version_mapping = get_version_mapping(pkg_info)
            split_combo, versions = find_optimal_split(version_mapping)
            status_dict[pkg]['status'][distro] = describe(split_combo, versions, pkg)
    return status_dict


def get_all_distros(d):
    distros = set()
    for pkg_dict in d.values():
        distros = distros.union(set(pkg_dict['status'].keys()))

    return sorted(distros, key=distro_sorter)
