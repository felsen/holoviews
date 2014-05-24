"""
ViewGroup, Collector and related classes offer optional functionality
for holding and collecting DataView objects.
"""
import time, uuid
import numpy as np
from collections import OrderedDict

import param
from .sheetviews import SheetView, SheetStack, CoordinateGrid, BoundingBox
from .views import GridLayout,Stack, View, NdMapping, Dimension


Time = Dimension("time", type=param.Dynamic.time_fn.time_type)


class ViewGroup(object):
    """
    A ViewGroup offers convenient, multi-level attribute access for
    collections of Views or Stacks. ViewGroups may also be merged
    together using the update method. Here is an example of adding a
    View to a ViewGroup and accessing it:

    >>> group = ViewGroup()
    >>> group.example.path = View('data1', name='view1')
    >>> group.example.path
    View('data1', label='', metadata={}, name='view1', title='{label}')
    """

    def __init__(self, label=None, parent=None):
        self.__dict__['parent'] = parent
        self.__dict__['label'] = label
        self.__dict__['children'] = []
        # Path items will only be populated at root node
        self.__dict__['path_items'] = OrderedDict()

        self.__dict__['_fixed'] = False
        fixed_error = 'ViewGroup attribute access disabled with fixed=True'
        self.__dict__['_fixed_error'] = fixed_error

    @property
    def fixed(self):
        "If fixed, no new paths can be created via attribute access"
        return self.__dict__['_fixed']

    @fixed.setter
    def fixed(self, val):
        self.__dict__['_fixed'] = val


    def grid(self, ordering='alphanumeric'):
        """
        Turn the ViewGroup into a GridLayout with the View object
        ordering specified by a list of labels or by the specified
        ordering mode ('alphanumeric' or 'insertion').
        """
        if ordering == 'alphanumeric':
            child_ordering = sorted(self.children)
        elif ordering == 'insertion':
            child_ordering = self.children
        else:
            child_ordering = ordering

        children = [self.__dict__[l] for l in child_ordering]
        return GridLayout(list(child for child in children if not isinstance(child, ViewGroup)))


    def update(self, other):
        """
        Updated the contents of the current ViewGroup with the
        contents of a second ViewGroup.
        """
        if self.parent is None:
            self.path_items.update(other.path_items)
        for label in other.children:
            item = other[label]
            if label not in self:
                self[label] = item
            else:
                self[label].update(item)


    def set_path(self, path, val):
        """
        Set the given value at the supplied path.
        """
        path = tuple(path)
        if len(path) > 1:
            viewgroup = self.__getattr__(path[0])
            viewgroup.set_path(path[1:], val)
        else:
            self.__setattr__(path[0], val)


    def _propagate(self, path, val):
        """
        Propagate the value to the root node.
        """
        if self.parent is None: # Root
            self.path_items[path] = val
        else:
            self.parent._propagate((self.label,)+path, val)


    def __setitem__(self, label, val):
        self.__setattr__(label, val)


    def __getitem__(self, label):
        """
        Access a child element by label.
        """
        return self.__dict__[label]


    def __setattr__(self, label, val):

        # Getattr is skipped for root and first set of children
        shallow = (self.parent is None or self.parent.parent is None)
        if label != 'fixed' and self.fixed and shallow:
            raise AttributeError(self._fixed_error)

        super(ViewGroup, self).__setattr__(label, val)

        if not (label.startswith('_') or label =='fixed' or label in self.children):
            self.children.append(label)
            self._propagate((label,), val)


    def __getattr__(self, label):
        """
        Access a label from the ViewGroup or generate a new ViewGroup
        with the chosen attribute path.
        """
        try:
            return super(ViewGroup, self).__getattr__(label)
        except AttributeError: pass

        if label.startswith('_'):   raise AttributeError(str(label))
        elif self.fixed==True:      raise AttributeError(self._fixed_error)

        if label in self.children:
            return self.__dict__[label]

        self.children.append(label)
        child_group = ViewGroup(label=label, parent=self)
        self.__dict__[label] = child_group
        return child_group


    def __repr__(self):
        return "<ViewGroup of %d items>" % len(self.children)


    def __contains__(self, name):
        return name in self.children or name in self.path_items



class Reference(object):
    """
    A Reference allows access to an object to be deferred until it is
    needed in the appropriate context. References are used by
    Collector to capture the state of an object at collection time.

    One particularly important property of references is that they
    should be pickleable. This means that you can pickle Collectors so
    that you can unpickle them in different environments and still
    collect from the required object.

    A Reference only needs to have a resolved_type property and a
    resolve method. The constructor will take some specification of
    where to find the target object (may be the object itself).
    """

    @property
    def resolved_type(self):
        """
        Returns the type of the object resolved by this references. If
        multiple types are possible, the return is a tuple of types.
        """
        raise NotImplementedError


    def resolve(self, container=None):
        """
        Return the referenced object. Optionally, a container may be
        passed in from which the object is to be resolved.
        """
        raise NotImplementedError



class ViewRef(Reference):
    """
    A ViewRef object is a Reference to a dataview object in a
    Viewgroup that may not exist when initialized. This makes it
    possible to schedule tasks for processing data not yet present.

    ViewRefs compose with the * operator to specify Overlays and also
    support slicing of the referenced view objects:

    >>> ref = ViewRef().example.path1 * ViewRef().example.path2

    >>> g = ViewGroup()
    >>> g.example.path1 = SheetView(np.random.rand(5,5))
    >>> g.example.path2 = SheetView(np.random.rand(5,5))
    >>> overlay = ref.resolve(g)
    >>> len(overlay)
    2

    Note that the operands of * must be distinct ViewRef objects.
    """
    def __init__(self, specification=[], slices=None):
        """
        The specification list contains n-tuples where the tuple
        elements are strings specifying one level of multi-level
        attribute access. For instance, ('a', 'b', 'c') indicates
        'a.b.c'. The overlay operator is applied between each n-tuple.
        """
        self.specification = specification
        self.slices = dict.fromkeys(specification) if slices is None else slices


    @property
    def resolved_type(self):
        return (View, Stack, CoordinateGrid)


    def _resolve_ref(self, ref, viewgroup):
        """
        Get the View referred to by a single reference tuple if the
        data exists, otherwise raise AttributeError.
        """
        obj = viewgroup
        for label in ref:
            if label in obj:
                obj= obj[label]
            else:
                info = ('.'.join(ref), label)
                raise AttributeError("Could not resolve %r at level %r" % info)
        return obj


    def resolve(self, viewgroup):
        """
        Resolve the current ViewRef object into the appropriate View
        object (if available).
        """
        overlaid_view = None
        for ref in self.specification:
            view = self._resolve_ref(ref, viewgroup)
            # Access specified slices for the view
            slc = self.slices.get(ref, None)
            view = view if slc is None else view[slc]

            if overlaid_view is None:
                overlaid_view = view
            else:
                overlaid_view = overlaid_view * view
        return overlaid_view


    def __getitem__(self, index):
        """
        Slice the referenced DataView.
        """
        for ref in self.specification:
            self.slices[ref] = index
        return self


    def __getattr__(self, label):
        """
        Multi-level attribute access on a ViewRef() object creates a
        reference with the same specified attribute access path.
        """
        try:
            return super(ViewRef, self).__getattr__(label)
        except AttributeError as e:
            if label.startswith('_') or len(self.specification) > 1:
                raise e
        if len(self.specification) == 0:
            self.specification = [(label,)]
        elif len(self.specification) == 1:
            self.specification = [self.specification[0] + (label,)]
        return self


    def __mul__(self, other):
        """
        ViewRef object can be composed in to overlays.
        """
        if id(self) == id(other):
            raise Exception("Please ensure that each operand are distinct ViewRef objects.")
        slices = dict(self.slices, **other.slices)
        return ViewRef(self.specification + other.specification, slices=slices)


    def __repr__(self):
        return "ViewRef(%r)" %  self.specification



class Aggregator(object):
    """
    An Aggregator takes an object and corresponding hook and when
    called with a ViewGroup, updates it with the output of the hook
    (given the object). The output of the hook should be a View or a
    ViewGroup.

    The input object may be a picklable object (e.g. a
    ParameterizedFunction) or a Reference to the target object.  The
    supplied *args and **kwargs are passed to the hook together with
    the resolved object.

    When mode is 'merge' the return value of the hook needs to be a
    ViewGroup to be merged with the viewgroup when called.
    """

    def __init__(self, obj, hook, mode, *args, **kwargs):
        self.obj = obj
        self.hook = hook
        self.mode=mode
        self.args=list(args)
        self.kwargs=kwargs
        self.path = None


    def _get_result(self, viewgroup, time, times):
        """
        Method returning a View or ViewGroup to be merged into the
        viewgroup (via the specified hook) in the call.
        """
        resolvable = hasattr(self.obj, 'resolve')
        obj = self.obj.resolve() if resolvable else self.obj
        return self.hook(obj, *self.args, **self.kwargs)


    def __call__(self, viewgroup, time=None, times=None):
        """
        Update and return the supplied ViewGroup with the output of
        the hook at the given time out of the given list of times.
        """
        if self.path is None:
            raise Exception("Aggregation path not set.")

        val = self._get_result(viewgroup, time, times)
        if val is None:  return viewgroup

        if self.mode == 'merge':
            if isinstance(val, ViewGroup):
                viewgroup.update(val)
                return viewgroup
            else:
                raise Exception("Return value is not a ViewGroup and mode is 'merge'.")

        if self.path not in viewgroup:
            if not isinstance(val, NdMapping):
                if val.title == '{label}':
                    val.title = ' '.join(self.path[::-1]) + val.title
                val = val.stack_type([((time,), val)], dimensions=[Time])
        else:
            current_val = viewgroup.path_items[self.path]
            val = self._merge_views(current_val, val, time)

        viewgroup.set_path(self.path,  val)
        return viewgroup


    def _merge_views(self, current_val, val, time):
        """
        Helper for merging views together. For instance, this method
        will add a SheetView to a SheetStack or merge two SheetStacks.
        """
        if isinstance(val, View):
            current_val[time] = val
        elif (isinstance(current_val, Stack) and 'time' not in current_val.dimension_labels):
            raise Exception("Time dimension is missing.")
        else:
            current_val.update(val)
        return current_val



class Analysis(Aggregator):
    """
    An Analysis is a type of Aggregator that updates a viewgroup with
    the results of a ViewOperation. Analysis takes a ViewRef object as
    input which is resolved to generate input for the ViewOperation.
    """

    def __init__(self, reference, analysis, stackwise=False, *args, **kwargs):
        self.reference = reference
        self.analysis = analysis

        self.args = list(args)
        self.kwargs = kwargs
        self.stackwise = stackwise
        self.mode = 'set'
        self.path = None


    def _get_result(self, viewgroup, time, times):
        if self.stackwise and time==times[-1]:
            view = self.reference.resolve(viewgroup)
            return self.analysis(view, *self.args, **self.kwargs)
        elif self.stackwise:
            return None
        else:
            view = self.reference.resolve(viewgroup)
            return self.analysis(view, *self.args, **self.kwargs)

    def __str__(self):
        return "Analysis(%r)" % self.analysis




class Collector(ViewGroup):
    """
    A Collector specifies a template for how to populate a ViewGroup
    with data over time. Two methods are used to schedule data
    collection: 'collect' and 'analyse'.

    The collect method takes an object (or reference) and collects
    views from it (as configured by setting an appropriate hook set
    with the for_type classmethod).

    The analysis method takes a reference to data on the viewgroup (a
    ViewRef) and passes the resolved output to the given analysisfn
    ViewOperation.

    >>> Collector.for_type(str, lambda x: View(x, name=x))

    >>> c = Collector()
    >>> c.target.path = c.collect('example string')

    # Start collection...
    >>> data = c(times=[1,2,3,4,5])
    >>> isinstance(data, ViewGroup)
    True
    >>> isinstance(data.target.path, Stack)
    True

    >>> times = data.target.path.keys()
    >>> print("Collected the data for %d time values" % len(times))
    Collected the data for 5 time values

    >>> data.target.path.last                 #doctest: +ELLIPSIS
    View('example string'...title='target path \\n Time = 5.0')
    """

    time_hook = param.Callable(param.Dynamic.time_fn.advance, doc="""
       A callable that advances by the specified time before the next
       batch of collection tasks is executed.""")

    time_fn = param.Callable(default=param.Dynamic.time_fn, doc="""
        A callable that returns the time where the time may be the
        simulation time or wall-clock time. The time values are
        recorded by the Stack keys.""")

    type_hooks = {}

    @classmethod
    def for_type(cls, tp, hookfn, referencer=None, mode='set'):
        """
        For an object of a given type, apply the hookfn and use the
        specified mode to aggregate the data.

        To allow pickling (or any other defered access) of the target
        object, a referencer (a Reference subclass) may be specified
        to wrap the object as required.

        If mode is 'merge', merge the ViewGroup output by the hook,
        otherwise if 'set', add the output to the path specified by
        the ViewRef.
        """
        cls.type_hooks[tp] = (hookfn, mode, referencer)


    @classmethod
    def select_hook(cls, obj):
        """
        Select the most appropriate hook by the most specific type.
        """
        matches = []
        obj_class = obj if isinstance(obj, type) else type(obj)

        if obj_class == param.parameterized.ParameterizedMetaclass:
            obj_class = obj

        for tp in cls.type_hooks.keys():
            if issubclass(obj_class, tp):
                matches.append(tp)

        if len(matches) == 0:
            raise Exception("No hook found for object of type %s"
                            % obj.__class__.__name__)

        for obj_cls in obj_class.mro():
            if obj_cls in matches:
                return cls.type_hooks[obj_cls]

        raise Exception("Match not in object classes mro()")


    def __init__(self, **kwargs):
        super(Collector,self).__init__(**kwargs)
        self.__dict__['refs'] = []
        self._scheduled_tasks = []

        fixed_error = 'Collector specification disabled after first call.'
        self.__dict__['_fixed_error'] = fixed_error


    @property
    def ref(self):
        """
        A convenient property to easily generate ViewRef object (via
        attribute access). Used to define View references for analysis
        or for setting a path for an Aggregator on the Collector.
        """
        new_ref = ViewRef()
        self.refs.append(new_ref)
        return new_ref


    def collect(self, obj, *args, **kwargs):
        """
        Aggregate views from the object at each step by passing the
        arguments to the corresponding hook. The object may represent
        itself, or it may be a Reference. If a referencer class was
        specified when the hook was defined, the object will
        automatically be wrapped into a reference.
        """
        resolveable = None
        if hasattr(obj, 'resolve'):
            resolveable = obj
            obj = obj.resolved_type

        hook, mode, resolver = self.select_hook(obj)
        if resolveable is None:
            resolveable = obj if resolver is None else resolver(obj)

        task = Aggregator(resolveable, hook, mode, *args, **kwargs)
        if mode == 'merge':
            self.path_items[uuid.uuid4().hex] = task
        return task


    def analyse(self, reference, analysisfn,  stackwise=False, *args, **kwargs):
        """
        Given a ViewRef and the ViewOperation analysisfn, process the
        data resolved by the reference with analysisfn at each step.
        """
        return Analysis(reference, analysisfn, stackwise=stackwise, *args, **kwargs)


    def __call__(self, viewgroup=ViewGroup(), times=[]):
        self.fixed = False
        viewgroup.fixed = False

        self._schedule_tasks()
        for t in np.diff([0]+times):
            self.time_hook(float(t))
            # An empty viewgroup buffer stops analysis repeatedly
            # computing results over the entire accumulated stack
            viewgroup_buffer = ViewGroup()
            for task in self._scheduled_tasks:
                if isinstance(task, Analysis) and task.stackwise:
                    task(viewgroup, self.time_fn(), times)
                else:
                    task(viewgroup_buffer, self.time_fn(), times)
                    viewgroup.update(viewgroup_buffer)

        self.fixed = True
        viewgroup.fixed = True
        return viewgroup


    def _schedule_tasks(self):
        """
        Inspect the path_items to find all the Aggregators that have
        been specified and add them to the scheduled tasks list.
        """
        self._scheduled_tasks = []
        for path, task in self.path_items.items():

            if not isinstance(task, (Aggregator, Analysis)):
                self._scheduled_tasks = []
                raise Exception("Only Aggregators or Analysis objects allowed, not %s" % task)

            if isinstance(path, tuple) and task.mode == 'merge':
                self._scheduled_tasks = []
                raise Exception("Setting path for Task that is in 'merge' mode.")
            task.path = path
            self._scheduled_tasks.append(task)


    def __repr__(self):
        return str(self)

    def __str__(self):
        return "Collector with %d tasks scheduled" % len(self._scheduled_tasks)