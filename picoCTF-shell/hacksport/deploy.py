"""
Problem deployment.
"""

from random import Random, randint
from abc import ABCMeta
from hashlib import md5
from imp import load_source
from pwd import getpwnam, getpwall
from json import loads
from jinja2 import Environment, Template, FileSystemLoader
from hacksport.problem import Remote, Compiled, File, ProtectedFile, ExecutableFile
from hacksport.operations import create_user
from hacksport.utils import sanitize_name, get_attributes

import os
import shutil

PROBLEM_FILES_DIR = "problem_files"

# TODO: move somewhere else
SECRET = "hacksports2015"

def challenge_meta(attributes):
    """
    Returns a metaclass that will introduce the given attributes into the class
    namespace.

    Args:
        attributes: The dictionary of attributes

    Returns:
        The metaclass described above
    """

    class ChallengeMeta(ABCMeta):
        def __new__(cls, name, bases, attr):
            attrs = dict(attr)
            attrs.update(attributes)
            return super().__new__(cls, name, bases, attrs)
    return ChallengeMeta

def update_problem_class(Class, problem_object, seed, user, instance_directory):
    """
    Changes the metaclass of the given class to introduce necessary fields before
    object instantiation.

    Args:
        Class: The problem class to be updated
        problem_name: The problem name
        seed: The seed for the Random object
        user: The linux username for this challenge instance
        instance_directory: The deployment directory for this instance

    Returns:
        The updated class described above
    """

    random = Random(seed)
    attributes = problem_object

    #url_for is a stub. Real implementation is placed before templating.
    #Calling it anywhere else is an error.
    def url_for_stub(_):
        raise Exception("url_for should only be called during templating operations.")

    attributes.update({"random": random, "user": user,
                       "directory": instance_directory, "url_for": url_for_stub})

    return challenge_meta(attributes)(Class.__name__, Class.__bases__, Class.__dict__)

def create_service_file(problem, instance_number, path):
    """
    Creates a systemd service file for the given problem

    Args:
        problem: the instantiated problem object
        instance_number: the instance number
        path: the location to drop the service file
    Returns:
        The path to the created service file
    """

    template = """[Unit]
Description={} instance

[Service]
Type={}
ExecStart={}

[Install]
WantedBy=multi-user.target"""

    problem_service_info = problem.service()
    converted_name = sanitize_name(problem.name)
    content = template.format(problem.name, problem_service_info['Type'], problem_service_info['ExecStart'])
    service_file_path = os.path.join(path, "{}_{}.service".format(converted_name, instance_number))

    with open(service_file_path, "w") as f:
        f.write(content)

    return service_file_path

def create_instance_user(problem_name, instance_number):
    """
    Generates a random username based on the problem name. The username returned is guaranteed to
    not exist.

    Args:
        problem_name: The name of the problem
        instance_number: The unique number for this instance
    Returns:
        A tuple containing the username and home directory
    """

    converted_name = sanitize_name(problem_name)
    username = "{}_{}".format(converted_name, instance_number)

    try:
        #Check if the user already exists.
        user = getpwnam(username)
        return username, user.pw_dir
    except KeyError:
        home_directory = create_user(username)
        return username, home_directory

def generate_seed(*args):
    """
    Generates a seed using the list of string arguments
    """

    return md5("".join(args).encode("utf-8")).hexdigest()

def generate_staging_directory(root="/tmp/staging/"):
    """
    Creates a random, empty staging directory

    Args:
        root: The parent directory for the new directory. Defaults to /tmp/staging/

    Returns:
        The path of the generated directory
    """

    if not os.path.isdir(root):
        os.makedirs(root)

    def get_new_path():
        path = os.path.join(root, str(randint(0, 1e12)))
        if os.path.isdir(path):
            return get_new_path()
        return path

    path = get_new_path()
    os.makedirs(path)
    return path

def template_string(template, **kwargs):
    """
    Templates the given string with the keyword arguments

    Args:
        template: The template string
        **kwards: Variables to use in templating
    """

    temp = Template(template)
    return temp.render(**kwargs)

def template_file(in_file_path, out_file_path, **kwargs):
    """
    Templates the given file with the keyword arguments.

    Args:
        in_file_path: The path to the template
        out_file_path: The path to output the templated file
        **kwargs: Variables to use in templating
    """

    env = Environment(loader=FileSystemLoader(os.path.dirname(in_file_path)))
    template = env.get_template(os.path.basename(in_file_path))
    output = template.render(**kwargs)

    with open(out_file_path, "w") as f:
        f.write(output)

def template_staging_directory(staging_directory, problem):
    """
    Templates every file in the staging directory recursively other than
    problem.json.

    Args:
        staging_directory: The path of the staging directory
        problem: The problem object
    """

    for root, dirnames, filenames in os.walk(staging_directory):
        for filename in filenames:
            if filename == "problem.json":
                continue
            fullpath = os.path.join(root, filename)
            try:
                template_file(fullpath, fullpath, **get_attributes(problem))
            except UnicodeDecodeError as e:
                # tried templating binary file
                pass

def deploy_files(staging_directory, instance_directory, file_list, username):
    """
    Copies the list of files from the staging directory to the instance directory.
    Will properly set permissions and setgid files based on their type.
    """

    # get uid and gid for root and problem user
    user = getpwnam(username)
    root = getpwnam("root")

    for f in file_list:
        output_path = os.path.join(instance_directory, os.path.basename(f.path))
        shutil.copy2(os.path.join(staging_directory, f.path), output_path)

        if isinstance(f, ProtectedFile) or isinstance(f, ExecutableFile):
            os.chown(output_path, root.pw_uid, user.pw_gid)
        else:
            os.chown(output_path, root.pw_uid, root.pw_gid)

        os.chmod(output_path, f.permissions)

def generate_instance(problem_object, problem_directory, instance_number, test_instance=False):
    """
    Runs the setup functions of Problem in the correct order

    Args:
        problem_object: The contents of the problem.json

    Returns:
        A tuple containing (problem, staging_directory, home_directory, files)
    """

    username, home_directory = create_instance_user(problem_object['name'], instance_number)
    seed = generate_seed(problem_object['name'], SECRET, str(instance_number))
    staging_directory = generate_staging_directory()
    copypath = os.path.join(staging_directory, PROBLEM_FILES_DIR)
    shutil.copytree(problem_directory, copypath)

    challenge = load_source("challenge", os.path.join(copypath, "challenge.py"))

    Problem = update_problem_class(challenge.Problem, problem_object, seed, username, home_directory)

    # store cwd to restore later
    cwd = os.getcwd()
    os.chdir(copypath)

    # run methods in proper order
    problem = Problem()
    problem.initialize()

    # reseed and generate flag
    problem.flag = problem.generate_flag(Random(seed))

    web_accessible_files = []
    def url_for(source):
        web_accessible_files += [source]
        return "http://" + source

    #Add real implementation
    problem.url_for = url_for

    template_staging_directory(staging_directory, problem)

    if isinstance(problem, Compiled):
        problem.compiler_setup()
    if isinstance(problem, Remote):
        problem.remote_setup()
    problem.setup()

    os.chdir(cwd)

    all_files = problem.files

    if isinstance(problem, Compiled):
        all_files.extend(problem.compiled_files)
    if isinstance(problem, Remote):
        all_files.extend(problem.remote_files)

    assert all([isinstance(f, File) for f in all_files])

    service = create_service_file(problem, instance_number, staging_directory)

    # template the description
    problem.description = template_string(problem.description, **get_attributes(problem))

    print(web_accessible_files)
    return problem, staging_directory, home_directory, all_files

def deploy_problem(problem_directory, instances=1):
    """
    Deploys the problem specified in problem_directory.

    Args:
        problem_directory: The directory storing the problem
        instances: The number of instances to deploy. Defaults to 1.

    Returns:
        TODO
    """

    object_path = os.path.join(problem_directory, "problem.json")

    with open(object_path, "r") as f:
        json_string = f.read()

    problem_object = loads(json_string)

    instance_list = []

    for instance_number in range(instances):
        print("Generating instance {}".format(instance_number))
        problem, staging_directory, home_directory, files = generate_instance(problem_object, problem_directory, instance_number)
        print("\tdesc={}\n\tflag={}\n\tstaging_directory={}\n\tfiles={}".format(problem.description, problem.flag, staging_directory, files))

        instance_list.append((problem, staging_directory, home_directory, files))

    # all instances generated without issue
    # let's deploy them now
    for problem, staging_directory, home_directory, files in instance_list:
        problem_path = os.path.join(staging_directory, PROBLEM_FILES_DIR)
        deploy_files(problem_path, home_directory, files, problem.user)
