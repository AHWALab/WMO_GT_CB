import os
import errno
from os import makedirs

def is_non_zero_file(fpath):
    """Function that checks if a file exists and is not empty

    Arguments:
        fpath {str} -- file path to check

    Returns:
        bool -- True or False
    """
    if os.path.isfile(fpath) and os.path.getsize(fpath) > 0:
        return True
    else:
        return False

def mkdir_p(path):
    """Function that makes a new directory.

    This function tries to make directories, ignoring errors if they exist.

    Arguments:
        path {str} -- path of folder to create
    """
    try:
        makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            pass
        else:
            raise

def newline(n=1):
    """Print n blank lines."""
    print("\n" * n, end="")