import lifesospy_mqtt.const as mqtt_const
import os

from setuptools import setup


def readme():
    with open(os.path.join(os.path.dirname(__file__), 'README.rst')) as f:
        return f.read()


setup(
    name=mqtt_const.PROJECT_NAME,
    version=mqtt_const.PROJECT_VERSION,
    description=mqtt_const.PROJECT_DESCRIPTION,
    long_description=readme(),
    packages=['lifesospy_mqtt'],
    install_requires=[
        'lifesospy~=0.10.0',
        'hbmqtt~=0.9.3',
        'pyyaml~=3.13',
        'python-dateutil~=2.7.3',
        'python-daemon~=2.1.2'],
    python_requires='>=3.5.3',
    author='Richard Orr',
    url='https://github.com/rorr73/lifesospy_mqtt',
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Environment :: Other Environment',
        'Intended Audience :: End Users/Desktop',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Topic :: Home Automation',
        'Topic :: Security',
    ],
    entry_points={
        'console_scripts': ['lifesospy_mqtt = lifesospy_mqtt.__main__:main']
    },
)