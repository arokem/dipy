""" Class to raise error for missing modules or other misfortunes
"""

class TripWireError(Exception):
    """ Exception if trying to use TripWire object """


def is_tripwire(obj):
    """ Returns True if `obj` appears to be a TripWire object

    Examples
    --------
    >>> is_tripwire(object())
    False
    >>> is_tripwire(TripWire('some message'))
    True
    """
    try:
        obj.any_attribute
    except TripWireError:
        return True
    except:
        pass
    return False


class TripWire(object):
    """ Class raising error if used

    Standard use is to proxy modules that we could not import

    Examples
    --------
    >>> try:
    ...     import silly_module_name
    ... except ImportError:
    ...    silly_module_name = TripWire('We do not have silly_module_name')
    >>> silly_module_name.do_silly_thing('with silly string') #doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
        ...
    TripWireError: We do not have silly_module_name
    """
    def __init__(self, msg):
        self._msg = msg

    def __getattr__(self, attr_name):
        ''' Raise informative error accessing attributes '''
        raise TripWireError(self._msg)

    def __call__(self, *args, **kargs):
        """Raise informative error when TripWire is called"""
        raise TripWireError(self._msg)

def defineif(condition, msg):
    """Replaces function with a TripWire if condition is False

    Examples
    --------
    >>> @defineif(True, 'this does nothing')
    ... def cow():
    ...    return 'moo'
    >>> cow()
    'moo'
    >>> @defineif(False, 'pig is a tripwire')
    ... def pig():
    ...     return 'oink'
    >>> pig()
    Traceback (most recent call last):
        ...
    TripWireError: pig is a tripwire

    """
    if condition:
        return lambda x : x
    else:
        return lambda x : TripWire(msg)
