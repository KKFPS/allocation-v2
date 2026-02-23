'''
setup.py
'''

import os
import shutil
import glob

from setuptools import setup
from setuptools import find_packages
from setuptools import Command

name = 'vehicle-route allocation model'
version = '0.0.2'
description = 'Vehicle Allocation'
author = 'Sofia Taylor and Ben Fletcher'
author_email = 'sofia.taylor@flexpowerltd.com'
CDIR = os.path.dirname(os.path.abspath(__file__))


def read_requirements():
    """
    Safely parse requirements from requirements.txt,
    skipping pip options (like --extra-index-url) and comments.
    """
    file_name = os.path.join('.', 'requirements.txt')
    requirements = []
    with open(file_name, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            # Skip empty lines, comments, and pip directives
            if not line or line.startswith('#') or line.startswith('--') or '--ignore-setup-py' in line:
                continue
            requirements.append(line)
    return requirements


class CleanCommand(Command):
    '''
    Custom clean command to tidy up the project root.
    '''
    clean_targets = [
        'build',
        'dist',
        '*.tgz',
        '*.zip',
        '*.egg-info',
        '__pycache__',
        '*/__pycache__',
        '*/*/__pycache__',
        '*.pyc',
        '*/*.pyc',
        '*/*/*.pyc',
    ]
    user_options = []

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    def run(self):
        for c_t in self.clean_targets:
            for tar in glob.glob(c_t):
                shutil.rmtree(tar)
                print('removed:', tar)


setup(
    name=name,
    version=version,
    description=description,
    author=author,
    author_email=author_email,
    install_requires=read_requirements(),
    packages=find_packages(exclude=('tests')),
    cmdclass={
        'clean': CleanCommand,
    },
)
