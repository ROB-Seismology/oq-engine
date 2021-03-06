# Copyright (c) 2010-2013, GEM Foundation.
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.


"""
Core functionality for the classical PSHA risk calculator.
"""

import random
import collections
import itertools
import numpy

from django import db

from openquake.hazardlib.geo import mesh
from openquake.risklib import scientific, workflows

from openquake.engine.calculators import post_processing
from openquake.engine.calculators.risk import (
    base, hazard_getters, validation, writers)
from openquake.engine.db import models
from openquake.engine import logs, writer
from openquake.engine.input import logictree
from openquake.engine.performance import EnginePerformanceMonitor
from openquake.engine.utils import tasks


@tasks.oqtask
def event_based(job_id, units, containers, params):
    """
    Celery task for the event based risk calculator.

    :param job_id: the id of the current
        :class:`openquake.engine.db.models.OqJob`
    :param dict units:
      A list of :class:`openquake.risklib.workflows.CalculationUnit` instances
    :param containers:
      An instance of :class:`..writers.OutputDict` containing
      output container instances (e.g. a LossCurve)
    :param params:
      An instance of :class:`..base.CalcParams` used to compute
      derived outputs
    :returns:
      A dictionary {loss_type: event_loss_table}
    """
    monitor = EnginePerformanceMonitor(
        None, job_id, event_based, tracing=True)

    # Do the job in other functions, such that they can be unit tested
    # without the celery machinery
    event_loss_tables = dict()

    with db.transaction.commit_on_success(using='job_init'):
        for unit in units:
            event_loss_tables[unit.loss_type] = do_event_based(
                unit, containers.with_args(loss_type=unit.loss_type),
                params, monitor.copy)
    return event_loss_tables


def do_event_based(unit, containers, params, profile):
    """
    See `event_based` for a description of the params

    :returns: the event loss table generated by `units`
    """
    outputs, stats = unit(profile('getting data'),
                          profile('computing individual risk'),
                          post_processing, params.quantiles)

    if not len(outputs):
        logs.LOG.info("Exit from task as no asset could be processed")
        return collections.Counter()

    for out in outputs:
        if params.sites_disagg:
            with profile('disaggregating results'):
                rupture_ids = out.output.event_loss_table.keys()
                disagg_outputs = disaggregate(out.output, rupture_ids, params)
        else:
            disagg_outputs = None

        with profile('saving individual risk'):
            save_individual_outputs(
                containers.with_args(hazard_output_id=out.hid),
                out.output, disagg_outputs, params)

    if stats is not None:
        with profile('saving risk statistics'):
            save_statistical_output(
                containers.with_args(hazard_output_id=None), stats, params)
        return stats.event_loss_table
    else:
        return outputs[0].output.event_loss_table


def save_individual_outputs(containers, outputs, disagg_outputs, params):
    """
    Save loss curves, loss maps and loss fractions associated with a
    calculation unit

    :param containers:
        a :class:`openquake.engine.calculators.risk.writers.OutputDict`
        instance holding the reference to the output container objects
    :param outputs:
        a :class:`openquake.risklib.workflows.ProbabilisticEventBased.Output`
        holding the output data for a calculation unit
    :param disagg_outputs:
        a :class:`.DisaggregationOutputs` holding the disaggreation
        output data for a calculation unit
    :param params:
        a :class:`openquake.engine.calculators.risk.base.CalcParams`
        holding the parameters for this calculation
    """

    containers.write(
        outputs.assets,
        (outputs.loss_curves, outputs.average_losses, outputs.stddev_losses),
        output_type="event_loss_curve")

    containers.write_all(
        "poe", params.conditional_loss_poes,
        outputs.loss_maps,
        outputs.assets,
        output_type="loss_map")

    if disagg_outputs is not None:
        # FIXME. We should avoid synthetizing the generator
        assets = list(disagg_outputs.assets_disagg)
        containers.write(
            assets,
            disagg_outputs.magnitude_distance,
            disagg_outputs.fractions,
            output_type="loss_fraction",
            variable="magnitude_distance")
        containers.write(
            assets,
            disagg_outputs.coordinate, disagg_outputs.fractions,
            output_type="loss_fraction",
            variable="coordinate")

    if outputs.insured_curves is not None:
        containers.write(
            outputs.assets,
            (outputs.insured_curves, outputs.average_insured_losses,
             outputs.stddev_insured_losses),
            output_type="event_loss_curve", insured=True)


def save_statistical_output(containers, stats, params):
    """
    Save statistical outputs (mean and quantile loss curves, mean and
    quantile loss maps) for the calculation.

    :param containers:
        a :class:`openquake.engine.calculators.risk.writers.OutputDict`
        instance holding the reference to the output container objects
    :param stats:
        :class:`openquake.risklib.workflows.ProbabilisticEventBased.StatisticalOutput`
        holding the statistical output data
    :param params:
        a :class:`openquake.engine.calculators.risk.base.CalcParams`
        holding the parameters for this calculation
    """

    containers.write(
        stats.assets, (stats.mean_curves, stats.mean_average_losses),
        output_type="loss_curve", statistics="mean")

    containers.write_all(
        "poe", params.conditional_loss_poes, stats.mean_maps,
        stats.assets, output_type="loss_map", statistics="mean")

    # quantile curves and maps
    containers.write_all(
        "quantile", params.quantiles,
        [(c, a) for c, a in itertools.izip(stats.quantile_curves,
                                           stats.quantile_average_losses)],
        stats.assets, output_type="loss_curve", statistics="quantile")

    if params.quantiles:
        for quantile, maps in zip(params.quantiles, stats.quantile_maps):
            containers.write_all(
                "poe", params.conditional_loss_poes, maps,
                stats.assets, output_type="loss_map",
                statistics="quantile", quantile=quantile)

    # mean and quantile insured curves
    if stats.mean_insured_curves is not None:
        containers.write(
            stats.assets, (stats.mean_insured_curves,
                           stats.mean_average_insured_losses),
            output_type="loss_curve", statistics="mean", insured=True)

        containers.write_all(
            "quantile", params.quantiles,
            [(c, a) for c, a in itertools.izip(
                stats.quantile_insured_curves,
                stats.quantile_average_insured_losses)],
            stats.assets,
            output_type="loss_curve", statistics="quantile", insured=True)


class DisaggregationOutputs(object):
    def __init__(self, assets_disagg, magnitude_distance,
                 coordinate, fractions):
        self.assets_disagg = assets_disagg
        self.magnitude_distance = magnitude_distance
        self.coordinate = coordinate
        self.fractions = fractions


def disaggregate(outputs, rupture_ids, params):
    """
    Compute disaggregation outputs given the individual `outputs` and `params`

    :param outputs:
      an instance of
      :class:`openquake.risklib.workflows.ProbabilisticEventBased.Output`
    :param params:
      an instance of :class:`..base.CalcParams`
    :param list rupture_ids:
      a list of :class:`openquake.engine.db.models.SESRupture` IDs
    :returns:
      an instance of :class:`DisaggregationOutputs`
    """
    def disaggregate_site(site, loss_ratios):
        for fraction, rupture_id in zip(loss_ratios, rupture_ids):
            rupture = models.SESRupture.objects.get(pk=rupture_id)
            s = rupture.surface
            m = mesh.Mesh(numpy.array([site.x]), numpy.array([site.y]), None)

            mag = numpy.floor(rupture.magnitude / params.mag_bin_width)
            dist = numpy.floor(
                s.get_joyner_boore_distance(m))[0] / params.distance_bin_width

            closest_point = iter(s.get_closest_points(m)).next()
            lon = closest_point.longitude / params.coordinate_bin_width
            lat = closest_point.latitude / params.coordinate_bin_width

            yield "%d,%d" % (mag, dist), "%d,%d" % (lon, lat), fraction

    assets_disagg = []
    disagg_matrix = []

    for asset, losses in zip(outputs.assets, outputs.loss_matrix):
        if asset.site in params.sites_disagg:
            disagg_matrix.extend(list(disaggregate_site(asset.site, losses)))

            # FIXME. the functions in
            # openquake.engine.calculators.risk.writers requires an
            # asset per each row in the disaggregation matrix. To this
            # aim, we repeat the assets that will be passed to such
            # functions
            assets_disagg = itertools.chain(
                assets_disagg,
                itertools.repeat(asset, len(rupture_ids)))

    if assets_disagg:
        magnitudes, coordinates, fractions = zip(*disagg_matrix)
    else:
        magnitudes, coordinates, fractions = [], [], []

    return DisaggregationOutputs(
        assets_disagg, magnitudes, coordinates, fractions)


class EventBasedRiskCalculator(base.RiskCalculator):
    """
    Probabilistic Event Based PSHA risk calculator. Computes loss
    curves, loss maps, aggregate losses and insured losses for a given
    set of assets.
    """

    #: The core calculation celery task function
    core_calc_task = event_based

    # FIXME(lp). Validate sites_disagg to ensure non-empty outputs
    validators = base.RiskCalculator.validators + [
        validation.RequireEventBasedHazard,
        validation.ExposureHasInsuranceBounds]

    output_builders = [writers.EventLossCurveMapBuilder,
                       writers.LossFractionBuilder]

    def __init__(self, job):
        super(EventBasedRiskCalculator, self).__init__(job)
        self.event_loss_tables = collections.defaultdict(collections.Counter)
        self.rnd = random.Random()
        self.rnd.seed(self.rc.master_seed)

        # seed the rng to generate different seeds per-each output
        # (i.e. each hazard realization). This allows different tasks
        # to generate the same random numbers given an output. These
        # seeds will be used when computing ground motion values on
        # the fly in order to provide the right correlation between
        # random numbers generated across tasks

        rnd = random.Random()
        rnd.seed(self.rc.master_seed)
        self.hazard_seeds = [rnd.randint(0, models.MAX_SINT_32)
                             for _ in self.rc.hazard_outputs()]

    def task_completed(self, event_loss_tables):
        """
        Updates the event loss table
        """
        self.log_percent(event_loss_tables)
        for loss_type in models.loss_types(self.risk_models):
            task_loss_table = event_loss_tables[loss_type]
            self.event_loss_tables[loss_type] += task_loss_table

    def post_process(self):
        """
          Compute aggregate loss curves and event loss tables
        """
        with EnginePerformanceMonitor('post processing', self.job.id):

            time_span, tses = self.hazard_times()
            for loss_type, event_loss_table in self.event_loss_tables.items():
                for hazard_output in self.rc.hazard_outputs():

                    event_loss = models.EventLoss.objects.create(
                        output=models.Output.objects.create_output(
                            self.job,
                            "Event Loss Table. type=%s, hazard=%s" % (
                                loss_type, hazard_output.id),
                            "event_loss"),
                        loss_type=loss_type,
                        hazard_output=hazard_output)
                    inserter = writer.CacheInserter(models.EventLossData, 9999)

                    rupture_ids = models.SESRupture.objects.filter(
                        ses__ses_collection__lt_realization=
                        hazard_output.output_container.lt_realization
                    ).values_list('id', flat=True)

                    for rupture_id in rupture_ids:
                        if rupture_id in event_loss_table:
                            inserter.add(
                                models.EventLossData(
                                    event_loss_id=event_loss.id,
                                    rupture_id=rupture_id,
                                    aggregate_loss=event_loss_table[
                                        rupture_id]))
                    inserter.flush()

                    aggregate_losses = [
                        event_loss_table[rupture_id]
                        for rupture_id in rupture_ids
                        if rupture_id in event_loss_table]

                    if aggregate_losses:
                        aggregate_loss_losses, aggregate_loss_poes = (
                            scientific.event_based(
                                aggregate_losses, tses=tses,
                                time_span=time_span,
                                curve_resolution=self.rc.loss_curve_resolution
                            ))

                        models.AggregateLossCurveData.objects.create(
                            loss_curve=models.LossCurve.objects.create(
                                aggregate=True, insured=False,
                                hazard_output=hazard_output,
                                loss_type=loss_type,
                                output=models.Output.objects.create_output(
                                    self.job,
                                    "aggregate loss curves. "
                                    "loss_type=%s hazard=%s" % (
                                        loss_type, hazard_output),
                                    "agg_loss_curve")),
                            losses=aggregate_loss_losses,
                            poes=aggregate_loss_poes,
                            average_loss=scientific.average_loss(
                                aggregate_loss_losses, aggregate_loss_poes),
                            stddev_loss=numpy.std(aggregate_losses))

    def calculation_unit(self, loss_type, assets):
        """
        :returns:
          a list of instances of `..base.CalculationUnit` for the given
          `assets` to be run in the celery task
        """

        # assume all assets have the same taxonomy
        taxonomy = assets[0].taxonomy
        risk_model = self.risk_models[taxonomy][loss_type]

        time_span, tses = self.hazard_times()

        # If we are computing ground motion values on the fly we need
        # logic trees
        if self.rc.hazard_outputs()[0].output_type == "ses":
            ltp = logictree.LogicTreeProcessor.from_hc(self.rc)
        else:
            ltp = None

        return workflows.CalculationUnit(
            loss_type,
            workflows.ProbabilisticEventBased(
                risk_model.vulnerability_function,
                self.rnd.randint(0, models.MAX_SINT_32),
                self.rc.asset_correlation,
                time_span, tses,
                self.rc.loss_curve_resolution,
                self.rc.conditional_loss_poes,
                self.rc.insured_losses),
            hazard_getters.GroundMotionValuesGetter(
                self.rc.hazard_outputs(),
                assets,
                self.rc.best_maximum_distance,
                risk_model.imt,
                self.hazard_seeds,
                ltp))

    def hazard_times(self):
        """
        Return the hazard investigation time related to the ground
        motion field and the so-called time representative of the
        stochastic event set
        """
        return (self.rc.investigation_time,
                self.hc.ses_per_logic_tree_path * self.hc.investigation_time)

    @property
    def calculator_parameters(self):
        """
        Calculator specific parameters
        """

        return base.make_calc_params(
            conditional_loss_poes=self.rc.conditional_loss_poes or [],
            quantiles=self.rc.quantile_loss_curves or [],
            insured_losses=self.rc.insured_losses,
            sites_disagg=self.rc.sites_disagg or [],
            mag_bin_width=self.rc.mag_bin_width,
            distance_bin_width=self.rc.distance_bin_width,
            coordinate_bin_width=self.rc.coordinate_bin_width)
