import os
import time
import copy

from pymc3 import Uniform, Model, Deterministic, Potential

from pyrocko import util

import numpy as num

import theano.tensor as tt
from theano import config as tconfig

from beat.utility import list2string, transform_sources, weed_input_rvs
from beat import sampler
from beat.models import geodetic, seismic, laplacian

from beat import config as bconfig
from beat.backend import ListArrayOrdering, ListToArrayBijection

from logging import getLogger

# disable theano rounding warning
tconfig.warn.round = False

km = 1000.

logger = getLogger('models')


__all__ = [
    'GeometryOptimizer',
    'DistributionOptimizer',
    'load_model']


class InconsistentNumberHyperparametersError(Exception):

    context = 'Configuration file has to be updated!' + \
              ' Hyperparameters have to be re-estimated. \n' + \
              ' Please run "beat update <project_dir>' + \
              ' --parameters=hypers, hierarchicals"'

    def __init__(self, errmess=''):
        self.errmess = errmess

    def __str__(self):
        return '\n%s\n%s' % (self.errmess, self.context)


geometry_composite_catalog = {
    'seismic': seismic.SeismicGeometryComposite,
    'geodetic': geodetic.GeodeticGeometryComposite}


distributer_composite_catalog = {
    'seismic': seismic.SeismicDistributerComposite,
    'geodetic': geodetic.GeodeticDistributerComposite,
    'laplacian': laplacian.LaplacianDistributerComposite}


interseismic_composite_catalog = {
    'geodetic': geodetic.GeodeticInterseismicComposite}


class Problem(object):
    """
    Overarching class for the optimization problems to be solved.

    Parameters
    ----------
    config : :class:`beat.BEATConfig`
        Configuration object that contains the problem definition.
    """
    _varnames = None
    _hypernames = None

    def __init__(self, config, hypers=False):

        self.model = None

        self._like_name = 'like'

        self.fixed_params = {}
        self.composites = {}
        self.hyperparams = {}

        logger.info('Analysing problem ...')
        logger.info('---------------------\n')

        # Load event
        if config.event is None:
            logger.warn('Found no event information!')
            raise AttributeError('Problem config has no event information!')
        else:
            self.event = config.event

        self.config = config

        mode = self.config.problem_config.mode

        outfolder = os.path.join(self.config.project_dir, mode)

        if hypers:
            outfolder = os.path.join(outfolder, 'hypers')

        self.outfolder = outfolder
        util.ensuredir(self.outfolder)

    def init_sampler(self, hypers=False):
        """
        Initialise the Sampling algorithm as defined in the configuration file.
        """

        if hypers:
            sc = self.config.hyper_sampler_config
        else:
            sc = self.config.sampler_config

        if self.model is None:
            raise Exception(
                'Model has to be built before initialising the sampler.')

        with self.model:
            if sc.name == 'Metropolis':
                logger.info(
                    '... Initiate Metropolis ... \n'
                    ' proposal_distribution: %s, tune_interval=%i,'
                    ' n_jobs=%i \n' % (
                        sc.parameters.proposal_dist,
                        sc.parameters.tune_interval,
                        sc.parameters.n_jobs))

                t1 = time.time()
                if hypers:
                    step = sampler.Metropolis(
                        n_chains=sc.parameters.n_chains,
                        likelihood_name=self._like_name,
                        tune_interval=sc.parameters.tune_interval,
                        proposal_name=sc.parameters.proposal_dist)
                else:
                    step = sampler.Metropolis(
                        n_chains=sc.parameters.n_chains,
                        tune_interval=sc.parameters.tune_interval,
                        likelihood_name=self._like_name,
                        proposal_name=sc.parameters.proposal_dist)
                t2 = time.time()
                logger.info('Compilation time: %f' % (t2 - t1))

            elif sc.name == 'SMC':
                logger.info(
                    '... Initiate Sequential Monte Carlo ... \n'
                    ' n_chains=%i, tune_interval=%i, n_jobs=%i,'
                    ' proposal_distribution: %s, \n' % (
                        sc.parameters.n_chains,
                        sc.parameters.tune_interval,
                        sc.parameters.n_jobs,
                        sc.parameters.proposal_dist))

                t1 = time.time()
                step = sampler.SMC(
                    n_chains=sc.parameters.n_chains,
                    tune_interval=sc.parameters.tune_interval,
                    coef_variation=sc.parameters.coef_variation,
                    proposal_dist=sc.parameters.proposal_dist,
                    likelihood_name=self._like_name)
                t2 = time.time()
                logger.info('Compilation time: %f' % (t2 - t1))

            elif sc.name == 'PT':
                logger.info(
                    '... Initiate Metropolis for Parallel Tempering... \n'
                    ' proposal_distribution: %s, tune_interval=%i,'
                    ' n_chains=%i \n' % (
                        sc.parameters.proposal_dist,
                        sc.parameters.tune_interval,
                        sc.parameters.n_chains))
                step = sampler.Metropolis(
                    n_chains=sc.parameters.n_chains,
                    likelihood_name=self._like_name,
                    tune_interval=sc.parameters.tune_interval,
                    proposal_name=sc.parameters.proposal_dist)

        return step

    def built_model(self):
        """
        Initialise :class:`pymc3.Model` depending on problem composites,
        geodetic and/or seismic data are included. Composites also determine
        the problem to be solved.
        """

        logger.info('... Building model ...\n')

        pc = self.config.problem_config

        with Model() as self.model:

            self.rvs, self.fixed_params = self.get_random_variables()

            self.init_hyperparams()

            total_llk = tt.zeros((1), tconfig.floatX)

            for datatype, composite in self.composites.items():
                if datatype in bconfig.modes_catalog[pc.mode].keys():
                    input_rvs = weed_input_rvs(
                        self.rvs, pc.mode, datatype=datatype)
                    fixed_rvs = weed_input_rvs(
                        self.fixed_params, pc.mode, datatype=datatype)

                else:
                    input_rvs = self.rvs
                    fixed_rvs = self.fixed_params

                total_llk += composite.get_formula(
                    input_rvs, fixed_rvs, self.hyperparams, pc)

            # deterministic RV to write out llks to file
            like = Deterministic('tmp', total_llk)

            # will overwrite deterministic name ...
            llk = Potential(self._like_name, like)
            logger.info('Model building was successful! \n')

    def plant_lijection(self):
        """
        Add list to array bijection to model object by monkey-patching.
        """
        if self.model is not None:
            lordering = ListArrayOrdering(
                self.model.unobserved_RVs, intype='tensor')
            lpoint = [var.tag.test_value for var in self.model.unobserved_RVs]
            self.model.lijection = ListToArrayBijection(lordering, lpoint)
        else:
            raise AttributeError('Model needs to be built!')

    def built_hyper_model(self):
        """
        Initialise :class:`pymc3.Model` depending on configuration file,
        geodetic and/or seismic data are included. Estimates initial parameter
        bounds for hyperparameters.
        """

        logger.info('... Building Hyper model ...\n')

        pc = self.config.problem_config

        if len(self.hierarchicals) == 0:
            self.init_hierarchicals()

        point = self.get_random_point(include=['hierarchicals', 'priors'])

        if self.config.problem_config.mode == bconfig.geometry_mode_str:
            for param in pc.priors.values():
                point[param.name] = param.testvalue

        with Model() as self.model:

            self.init_hyperparams()

            total_llk = tt.zeros((1), tconfig.floatX)

            for composite in self.composites.values():
                if hasattr(composite, 'analyse_noise'):
                    composite.analyse_noise(point)
                    composite.init_weights()

                composite.update_llks(point)

                total_llk += composite.get_hyper_formula(self.hyperparams)

            like = Deterministic('tmp', total_llk)
            llk = Potential(self._like_name, like)
            logger.info('Hyper model building was successful!')

    def get_random_point(self, include=['priors', 'hierarchicals', 'hypers']):
        """
        Get random point in solution space.
        """
        pc = self.config.problem_config

        point = {}
        if 'hierarchicals' in include:
            for name, param in self.hierarchicals.items():
                if not isinstance(param, num.ndarray):
                    point[name] = param.random()

        if 'priors' in include:
            for param in pc.priors.values():
                dimension = bconfig.get_parameter_shape(param, pc)
                point[param.name] = param.random(dimension=dimension)

        if 'hypers' in include:
            if len(self.hyperparams) == 0:
                self.init_hyperparams()

            hps = {hp_name: param.random()
                   for hp_name, param in self.hyperparams.items()
                   if not isinstance(param, num.ndarray)}

            point.update(hps)

        return point

    def get_random_variables(self):
        """
        Evaluate problem setup and return random variables dictionary.
        Has to be executed in a "with model context"!

        Returns
        -------
        rvs : dict
            variable random variables
        fixed_params : dict
            fixed random parameters
        """
        pc = self.config.problem_config

        logger.debug('Optimization for %i sources', pc.n_sources)

        rvs = dict()
        fixed_params = dict()
        for param in pc.priors.values():
            if not num.array_equal(param.lower, param.upper):

                shape = bconfig.get_parameter_shape(param, pc)

                kwargs = dict(
                    name=param.name,
                    shape=shape,
                    lower=param.lower,
                    upper=param.upper,
                    testval=param.testvalue,
                    transform=None,
                    dtype=tconfig.floatX)

                try:
                    rvs[param.name] = Uniform(**kwargs)

                except TypeError:
                    kwargs.pop('name')
                    rvs[param.name] = Uniform.dist(**kwargs)

            else:
                logger.info(
                    'not solving for %s, got fixed at %s' % (
                        param.name,
                        list2string(param.lower.flatten())))
                fixed_params[param.name] = param.lower

        return rvs, fixed_params

    @property
    def varnames(self):
        """
        Sampled random variable names.

        Returns
        -------
        list of strings
        """
        if self._varnames is None:
            self._varnames = list(self.get_random_variables()[0].keys())
        return self._varnames

    @property
    def hypernames(self):
        """
        Sampled random variable names.

        Returns
        -------
        list of strings
        """
        if self._hypernames is None:
            self.init_hyperparams()
        return self._hypernames

    def init_hyperparams(self):
        """
        Evaluate problem setup and return hyperparameter dictionary.
        """
        pc = self.config.problem_config
        hyperparameters = copy.deepcopy(pc.hyperparameters)

        hyperparams = {}
        n_hyp = 0
        modelinit = True
        self._hypernames = []
        for datatype, composite in self.composites.items():
            hypernames = composite.get_hypernames()

            for hp_name in hypernames:
                if hp_name in hyperparameters.keys():
                    hyperpar = hyperparameters.pop(hp_name)
                    if composite.config:   # only data composites
                        if composite.config.dataset_specific_residual_noise_estimation:
                            if datatype == 'seismic':
                                raise NotImplementedError('Not fully implemented!')
                                # TODO: fix this needs to be wavemap stations specific
                            else:
                                ndata = len(composite.get_unique_stations())
                        else:
                            ndata = 1
                    else:
                        ndata = 1
                else:
                    raise InconsistentNumberHyperparametersError(
                        'Datasets and -types require additional '
                        ' hyperparameter(s): %s!' % hp_name)

                if not num.array_equal(hyperpar.lower, hyperpar.upper):
                    dimension = hyperpar.dimension * ndata

                    kwargs = dict(
                        name=hyperpar.name,
                        shape=dimension,
                        lower=num.repeat(hyperpar.lower, ndata),
                        upper=num.repeat(hyperpar.upper, ndata),
                        testval=num.repeat(hyperpar.testvalue, ndata),
                        dtype=tconfig.floatX,
                        transform=None)

                    try:
                        hyperparams[hp_name] = Uniform(**kwargs)

                    except TypeError:
                        kwargs.pop('name')
                        hyperparams[hp_name] = Uniform.dist(**kwargs)
                        modelinit = False

                    n_hyp += dimension
                    self._hypernames.append(hyperpar.name)
                else:
                    logger.info(
                        'not solving for %s, got fixed at %s' % (
                            hyperpar.name,
                            list2string(hyperpar.lower.flatten())))
                    hyperparams[hyperpar.name] = hyperpar.lower

        if len(hyperparameters) > 0:
            raise InconsistentNumberHyperparametersError(
                'There are hyperparameters in config file, which are not'
                ' covered by datasets/datatypes.')

        if modelinit:
            logger.info('Optimization for %i hyperparameters in total!', n_hyp)

        self.hyperparams = hyperparams

    def update_llks(self, point):
        """
        Update posterior likelihoods of each composite of the problem with
        respect to one point in the solution space.

        Parameters
        ----------
        point : dict
            with numpy array-like items and variable name keys
        """
        for composite in self.composites.values():
            composite.update_llks(point)

    def apply(self, problem):
        """
        Update composites in problem object with given composites.
        """
        for composite in problem.composites.values():
            self.composites[composite.name].apply(composite)

    def point2sources(self, point):
        """
        Update composite sources(in place) with values from given point.

        Parameters
        ----------
        point : :func:`pymc3.Point`
            Dictionary with model parameters, for which the sources are
            updated
        """
        for composite in self.composites.values():
            self.composites[composite.name].point2sources(point)

    def update_weights(self, point, n_jobs=1, plot=False):
        """
        Calculate and update model prediction uncertainty covariances of
        composites due to uncertainty in the velocity model with respect to
        one point in the solution space. Shared variables are updated in place.

        Parameters
        ----------
        point : :func:`pymc3.Point`
            Dictionary with model parameters, for which the covariance matrixes
            with respect to velocity model uncertainties are calculated
        n_jobs : int
            Number of processors to use for calculation of seismic covariances
        plot : boolean
            Flag for opening the seismic waveforms in the snuffler
        """
        for composite in self.composites.values():
            composite.update_weights(point, n_jobs=n_jobs)

    def get_synthetics(self, point, **kwargs):
        """
        Get synthetics for given point in solution space.

        Parameters
        ----------
        point : :func:`pymc3.Point`
            Dictionary with model parameters
        kwargs especially to change output of seismic forward model
            outmode = 'traces'/ 'array' / 'data'

        Returns
        -------
        Dictionary with keys according to composites containing the synthetics
        as lists.
        """

        d = dict()

        for composite in self.composites.values():
            d[composite.name] = composite.get_synthetics(point, outmode='data')

        return d

    def init_hierarchicals(self):
        """
        Initialise hierarchical random variables of all composites.
        """
        for composite in self.composites.values():
            try:
                composite.init_hierarchicals(self.config.problem_config)
            except AttributeError:
                pass

    @property
    def hierarchicals(self):
        """
        Return dictionary of all hierarchical variables of the problem.
        """
        d = {}
        for composite in self.composites.values():
            if composite.hierarchicals is not None:
                d.update(composite.hierarchicals)

        return d


class SourceOptimizer(Problem):
    """
    Defines the base-class setup involving non-linear fault geometry.

    Parameters
    ----------
    config : :class:'config.BEATconfig'
        Contains all the information about the model setup and optimization
        boundaries, as well as the sampler parameters.
    """

    def __init__(self, config, hypers=False):

        super(SourceOptimizer, self).__init__(config, hypers)

        pc = config.problem_config

        # Init sources
        self.sources = []
        for i in range(pc.n_sources):
            if self.event:
                source = \
                    bconfig.source_catalog[pc.source_type].from_pyrocko_event(
                        self.event)

                source.stf = bconfig.stf_catalog[pc.stf_type](
                    duration=self.event.duration)

                # hardcoded inversion for hypocentral time
                if source.stf is not None:
                    source.stf.anchor = -1.
            else:
                source = bconfig.source_catalog[pc.source_type]()

            self.sources.append(source)


class GeometryOptimizer(SourceOptimizer):
    """
    Defines the model setup to solve for the non-linear fault geometry.

    Parameters
    ----------
    config : :class:'config.BEATconfig'
        Contains all the information about the model setup and optimization
        boundaries, as well as the sampler parameters.
    """

    def __init__(self, config, hypers=False):
        logger.info('... Initialising Geometry Optimizer ... \n')

        super(GeometryOptimizer, self).__init__(config, hypers)

        pc = config.problem_config

        dsources = transform_sources(
            self.sources,
            pc.datatypes,
            pc.decimation_factors)

        for datatype in pc.datatypes:
            self.composites[datatype] = geometry_composite_catalog[datatype](
                config[datatype + '_config'],
                config.project_dir,
                dsources[datatype],
                self.event,
                hypers)

        self.config = config

        # updating source objects with test-value in bounds
        tpoint = pc.get_test_point()
        self.point2sources(tpoint)


class InterseismicOptimizer(SourceOptimizer):
    """
    Uses the backslip-model in combination with the blockmodel to formulate an
    interseismic model.

    Parameters
    ----------
    config : :class:'config.BEATconfig'
        Contains all the information about the model setup and optimization
        boundaries, as well as the sampler parameters.
    """

    def __init__(self, config, hypers=False):
        logger.info('... Initialising Interseismic Optimizer ... \n')

        super(InterseismicOptimizer, self).__init__(config, hypers)

        pc = config.problem_config

        if pc.source_type == 'RectangularSource':
            dsources = transform_sources(
                self.sources,
                pc.datatypes)
        else:
            raise TypeError('Interseismic Optimizer has to be used with'
                            ' RectangularSources!')

        for datatype in pc.datatypes:
            self.composites[datatype] = \
                interseismic_composite_catalog[datatype](
                    config[datatype + '_config'],
                    config.project_dir,
                    dsources[datatype],
                    self.event,
                    hypers)

        self.config = config

        # updating source objects with fixed values
        point = self.get_random_point()
        self.point2sources(point)


class DistributionOptimizer(Problem):
    """
    Defines the model setup to solve the linear slip-distribution and
    returns the model object.

    Parameters
    ----------
    config : :class:'config.BEATconfig'
        Contains all the information about the model setup and optimization
        boundaries, as well as the sampler parameters.
    """

    def __init__(self, config, hypers=False):
        logger.info('... Initialising Distribution Optimizer ... \n')

        super(DistributionOptimizer, self).__init__(config, hypers)

        for datatype in config.problem_config.datatypes:
            data_config = config[datatype + '_config']

            composite = distributer_composite_catalog[
                datatype](
                    data_config,
                    config.project_dir,
                    self.event,
                    hypers)

            composite.set_slip_varnames(self.varnames)
            self.composites[datatype] = composite

        regularization = config.problem_config.mode_config.regularization
        try:
            composite = distributer_composite_catalog[
                regularization](config.project_dir, hypers)

            composite.set_slip_varnames(self.varnames)
            self.composites[regularization] = composite
        except KeyError:
            logger.info('Using "%s" regularization ...' % regularization)

        self.config = config

    def lsq_solution(self, point):
        """
        Returns non-negtive least-squares solution for given input point.

        Parameters
        ----------
        point : dict
            in solution space

        Returns
        -------
        point with least-squares solution
        """
        from scipy.optimize import nnls

        if self.config.problem_config.mode_config.regularization != \
                'laplacian':
            raise ValueError(
                'Least-squares- solution for distributed slip is only '
                'available with laplacian regularization!')

        lc = self.composites['laplacian']
        slip_varnames = ['uparr']

        for var in slip_varnames:
            if var not in self.varnames:
                raise ValueError(
                    'Distributed slip is only available for "uparr",'
                    ' which was fixed in the setup!')

        Gs = []
        ds = []
        for datatype, composite in self.composites.items():
            if datatype == 'geodetic':
                crust_ind = composite.config.gf_config.reference_model_idx
                keys = [composite.get_gflibrary_key(
                    crust_ind=crust_ind, wavename='static', component=var)
                    for var in slip_varnames]
                Gs.extend([composite.gfs[key]._gfmatrix for key in keys])
                ds.append(composite.sdata.get_value())

            elif datatype == 'seismic':
                logger.warning(
                    'Least-squares initialization is not'
                    ' supported (yet) for seismic data!')
                if False:
                    for wmap in composite.wavemaps:
                        keys = [composite.get_gflibrary_key(
                            crust_ind=crust_ind,
                            wavename=wmap.name, component=var)
                            for var in slip_varnames]
                        Gs.extend(
                            [composite.gfs[key]._gfmatrix for key in keys])
                        ds.append(wmap._prepared_data)

        if len(Gs) == 0:
            raise ValueError(
                'No Greens Function matrix available!'
                ' (needs geodetic datatype!)')

        G = num.vstack(Gs)
        D = num.vstack([lc.smoothing_op for sv in slip_varnames]) * \
            point[bconfig.hyper_name_laplacian] ** 2.

        dzero = num.zeros(D.shape[1], dtype=tconfig.floatX)
        A = num.hstack([G, D])
        d = num.hstack(ds + [dzero])

        # m, rmse, rankA, singularsA =  num.linalg.lstsq(A.T, d, rcond=None)
        m, res = nnls(A.T, d)
        npatches = self.config.problem_config.mode_config.npatches
        for i, var in enumerate(slip_varnames):
            point[var] = m[i * npatches: (i + 1) * npatches]

        point['uperp'] = dzero
        return point


problem_modes = list(bconfig.modes_catalog.keys())
problem_catalog = {
    problem_modes[0]: GeometryOptimizer,
    problem_modes[1]: DistributionOptimizer,
    problem_modes[2]: InterseismicOptimizer}


def load_model(project_dir, mode, hypers=False, build=True):
    """
    Load config from project directory and return BEAT problem including model.

    Parameters
    ----------
    project_dir : string
        path to beat model directory
    mode : string
        problem name to be loaded
    hypers : boolean
        flag to return hyper parameter estimation model instead of main model.
    build : boolean
        flag to build models

    Returns
    -------
    problem : :class:`Problem`
    """

    config = bconfig.load_config(project_dir, mode)

    pc = config.problem_config

    if hypers and len(pc.hyperparameters) == 0:
        raise ValueError(
            'No hyperparameters specified!'
            ' option --hypers not applicable')

    if pc.mode in problem_catalog.keys():
        problem = problem_catalog[pc.mode](config, hypers)
    else:
        logger.error('Modeling problem %s not supported' % pc.mode)
        raise ValueError('Model not supported')

    if build:
        if hypers:
            problem.built_hyper_model()
        else:
            problem.built_model()

    return problem
