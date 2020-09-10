#!/usr/bin/env python

import logging
import os
import re
import shutil
import sys
from pathlib import Path
from ruamel.yaml import YAML

from ansible.errors import AnsibleParserError
from ansible.parsing.dataloader import DataLoader
from ansible.parsing.mod_args import ModuleArgsParser
from ansible.parsing.yaml.objects import AnsibleMapping, AnsibleSequence

if os.environ.get("LSR_DEBUG") == "true":
    logging.getLogger().setLevel(logging.DEBUG)


ROLE_DIRS = ["defaults", "examples", "files", "handlers", "library", "meta",
    "module_utils", "tasks", "templates", "tests", "vars"]

PLAY_KEYS = {
    "gather_facts",
    "handlers",
    "hosts",
    "import_playbook",
    "post_tasks",
    "pre_tasks",
    "roles"
    "tasks",
}

TASK_LIST_KWS = ["always", "block", "handlers", "post_tasks", "pre_tasks", "rescue", "tasks"]

class LSRException(Exception):
    pass

def get_role_dir(role_path, dirpath):
    if role_path == dirpath:
        return None
    dir_pth = Path(dirpath)
    relpath = dir_pth.relative_to(role_path)
    base_dir = relpath.parts[0]
    if base_dir in ROLE_DIRS:
        return base_dir
    return None

def get_file_type(item):
    if isinstance(item, AnsibleMapping):
        if "galaxy_info" in item or "dependencies" in item:
            return "meta"
        return "vars"
    elif isinstance(item, AnsibleSequence):
        return "tasks"
    else:
        raise LSRException(f"Error: unknown type of file: {item}")


def get_item_type(item):
    if isinstance(item, AnsibleMapping):
        for key in PLAY_KEYS:
            if key in item:
                return "play"
        if "block" in item:
            return "block"
        return "task"
    else:
        raise LSRException(f"Error: unknown type of item: {item}")

class LSRFileTransformer(object):

    INDENT_RE = re.compile(r'^  (?! *#)', flags=re.MULTILINE)
    HEADER_RE = re.compile(r'^(.*---\n)', flags=re.DOTALL)
    FOOTER_RE = re.compile(r'\n([.][.][.].*)$', flags=re.DOTALL)

    def __init__(self, filepath, rolename, role_modules):
        dl = DataLoader()
        self.ans_data = dl.load_from_file(filepath)
        if self.ans_data is None:
            raise LSRException(f"file is empty {filepath}")
        self.file_type = get_file_type(self.ans_data)
        self.rolename = rolename
        self.role_modules = role_modules
        buf = open(filepath).read()
        self.ruamel_yaml = YAML(typ='rt')
        match = re.search(LSRFileTransformer.HEADER_RE, buf)
        if match:
            self.header = match.group(1)
        else:
            self.header = ''
        match = re.search(LSRFileTransformer.FOOTER_RE, buf)
        if match:
            self.footer = match.group(1)
        else:
            self.footer = ''
        self.ruamel_yaml.default_flow_style = False
        self.ruamel_yaml.preserve_quotes = True
        self.ruamel_yaml.width = None
        self.ruamel_data = self.ruamel_yaml.load(buf)
        self.ruamel_yaml.indent(mapping=2, sequence=4, offset=2)

    def run(self):
        if self.file_type == "vars":
            self.handle_vars(self.ans_data, self.ruamel_data)
        elif self.file_type == "meta":
            self.handle_meta(self.ans_data, self.ruamel_data)
        else:
            for a_item, ru_item in zip(self.ans_data, self.ruamel_data):
                ans_type = get_item_type(a_item)
                self.handle_vars(a_item, ru_item)
                self.handle_other(a_item, ru_item)
                if ans_type == "task":
                    self.handle_task(a_item, ru_item)
                self.handle_tasks(a_item, ru_item)

    def write(self, outputfile=None):
        def xform(thing):
            logging.debug(f"xform thing {thing}")
            if self.file_type == "tasks":
                thing = re.sub(LSRFileTransformer.INDENT_RE, '', thing)
            thing = self.header + thing
            if not thing.endswith("\n"):
                thing = thing + "\n"
            thing = thing + self.footer
            return thing
        if outputfile is None:
            outstrm = sys.stdout
        else:
            outstrm = open(outputfile, "w")
        self.ruamel_yaml.dump(self.ruamel_data, outstrm, transform=xform)

    def change_role(self, role):
        """this is a role specifier from a list e.g. the roles keyword.
        The role may be specified as a string rolename, or a dict where
        the rolename is specified as the value of the role or name key."""
        if isinstance(role, dict):
            if "name" in role:
                key = "name"
            else:
                key = "role"
            if role[key] == self.rolename or role[key] == "linux-system-roles." + self.rolename:
                role[key] = "fedora.system_roles." + self.rolename
        elif role == self.rolename or role == "linux-system-roles." + self.rolename:
            role = "fedora.system_roles." + self.rolename
        return role

    def handle_other(self, a_item, ru_item):
        """handle properties of Ansible item other than vars and tasks"""
        for idx, role in enumerate(ru_item.get("roles", [])):
            logging.debug(f"\troles item role {role}")
            ru_item["roles"][idx] = self.change_role(role)
        return

    def handle_vars(self, a_item, ru_item):
        """handle vars of Ansible item"""
        for var in a_item.get("vars", []):
            logging.debug(f"\tvar = {var}")
        return

    def handle_meta(self, a_item, ru_item):
        """handle meta/main.yml file"""
        for idx, role in enumerate(ru_item.get("dependencies", [])):
            logging.debug(f"\tmeta dependencies role {role}")
            ru_item["dependencies"][idx] = self.change_role(role)

    def handle_task(self, a_task, ru_task):
        """handle a single task"""
        mod_arg_parser = ModuleArgsParser(a_task)
        try:
            action, _, _ = mod_arg_parser.parse(skip_action_validation=True)
        except AnsibleParserError as e:
            raise LSRException("Couldn't parse task at %s (%s)\n%s" % (a_task.ansible_pos, e.message, a_task))
        if action == "include_role" or action == "import_role":
            logging.debug(f"\ttask role {a_task[action]['name']}")
            if ru_task[action]["name"] == self.rolename or ru_task[action]["name"] == "linux-system-roles." + self.rolename:
                ru_task[action]["name"] = "fedora.system_roles." + self.rolename
        elif action in self.role_modules:
            logging.debug(f"\ttask role module {action}")
            # assumes ru_task is an orderreddict
            idx = tuple(ru_task).index(action)
            val = ru_task.pop(action)
            ru_task.insert(idx, "fedora.system_roles." + action, val)
        self.handle_tasks(a_task, ru_task)

    def handle_task_list(self, a_tasks, ru_tasks):
        """item is a list of Ansible Task objects"""
        for a_task, ru_task in zip(a_tasks, ru_tasks):
            if "block" in a_task:
                self.handle_tasks(a_task, ru_task)
            else:
                self.handle_task(a_task, ru_task)
    
    def handle_tasks(self, a_item, ru_item):
        """item has one or more fields which hold a list of Task objects"""
        for kw in TASK_LIST_KWS:
            if kw in a_item:
                self.handle_task_list(a_item[kw], ru_item[kw])


def parse_role(role_path):
    role_modules = set()
    library_path = Path(os.path.join(role_path, "library"))
    if library_path.is_dir():
        for mod_file in library_path.iterdir():
            if mod_file.is_file() and mod_file.stem != "__init__":
                role_modules.add(mod_file.stem)
    rolename = library_path.parent.stem
    for (dirpath, _, filenames) in os.walk(role_path):
        role_dir = get_role_dir(role_path, dirpath)
        if not role_dir:
            continue
        for filename in filenames:
            if not filename.endswith(".yml"):
                continue
            filepath = os.path.join(dirpath, filename)
            logging.debug(f"filepath {filepath}")
            out_filepath = filepath + ".lsrout"
            try:
                lsrft = LSRFileTransformer(filepath, rolename, role_modules)
                lsrft.run()
                lsrft.write(out_filepath)
            except LSRException:
                logging.debug(f"Could not transform {filepath}")
                shutil.copyfile(filepath, out_filepath)


for role_path in sys.argv[1:]:
    parse_role(role_path)
