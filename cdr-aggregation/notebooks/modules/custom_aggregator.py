# Load modules depending whether we are on docker or on databricks
import os
if os.environ['HOME'] != '/root':
    from modules.priority_aggregator import *
    from modules.import_packages import *
    from modules.utilities import *
else:
    databricks = True

class custom_aggregator(priority_aggregator):
    """This class inherits from the priority aggregator class.
    In this class we handle special aggregations not designed for wider use
    Attributes
    ----------
    [check inherited attributes described in aggregator class]
    incidence : a pyspark dataframe. Incididence observed; for admin2 only
    Methods
    -------
    methods to implement special aggregations not designed for wider use
    """
    def __init__(self,
                 result_stub,
                 datasource,
                 regions,
                 re_create_vars = False):

        # initiate with parent init
        super().__init__(result_stub,datasource,regions)

        # for admin 2, we also have an incidence file
        if self.level == 'admin2':
            try:
                self.incidence = getattr(datasource, 'admin2_incidence')
            except Exception as e:
                print('No incidence file added.')
                pass


##### Non-priority Indicators - not used at the moment, but kept just in case.
# Mostly for internal use by the WB Covid19 Task Force, hence documentation is
# not yet very detailed.

    def origin_destination_matrix(self, time_filter, frequency):
      result = self.df.where(time_filter)\
        .where(F.col('region_lag') != F.col('region'))\
        .groupby(frequency, 'region', 'region_lag')\
        .agg(F.count(F.col('msisdn')).alias('count'))
      return result

    def origin_destination_unique_users_matrix(self, time_filter, frequency):
      result = self.df.where(time_filter)\
        .where(F.col('region_lag') != F.col('region'))\
        .groupby(frequency, 'region', 'region_lag')\
        .agg(F.countDistinct(F.col('msisdn')).alias('count'))
      return result

    def percent_residents_day_equal_night_location(self, time_filter, frequency):
      user_day_window = Window.partitionBy('msisdn', 'call_date')
      user_day_night_window = Window.partitionBy('msisdn',
        'home_region', 'call_date', frequency).orderBy('day_night')
      result = self.df.where(time_filter)\
        .withColumn('day_night',
            F.when((F.col('hour_of_day') < 9) | (F.col('hour_of_day') > 17),
            1).otherwise(0))\
        .withColumn('night_day',
            F.when((F.col('hour_of_day') > 9) & (F.col('hour_of_day') < 17),
            1).otherwise(0))\
        .withColumn('day_and_night',
            F.when((F.sum(F.col('day_night')).over(user_day_window) > 0) &\
                             (F.sum(F.col('night_day')).over(user_day_window) > 0),
                             1).otherwise(0))\
        .where(F.col('day_and_night') == 1)\
        .groupby('msisdn',
                 'home_region',
                 'call_date',
                 frequency,
                 'day_night',
                 'region')\
        .agg(F.count('location_id').alias('region_count'))\
        .orderBy('region_count')\
        .groupby('msisdn', 'home_region', 'call_date', frequency, 'day_night')\
        .agg(F.last('region_count').alias('max_region'))\
        .withColumn('day_equal_night', F.when(F.col('max_region') == \
            F.lag('max_region').over(user_day_night_window), 1).otherwise(0))\
        .where(F.col('day_night') == 1)\
        .groupby('home_region', frequency)\
        .agg(F.sum('day_equal_night').alias('day_equal_night_count'),
             F.count('day_equal_night').alias('total'))\
        .withColumn('pct_day_is_night',
            F.col('day_equal_night_count') / F.col('total'))
      return result

    def median_distance(self, time_filter, frequency):
      prep = self.df.where(time_filter)
      prep = prep.withColumn('location_id_lag',
        F.lag('location_id').over(user_window))
      prep = prep.join(self.distances_df,
             (prep.location_id==self.distances_df.destination) &\
             (prep.location_id_lag==self.distances_df.origin),
             'left')\
        .groupby('msisdn', 'home_region', frequency)\
        .agg(F.sum('distance').alias('distance'))
      prep.createOrReplaceTempView("df")
      result = self.spark.sql("select {}, home_region, \
      percentile_approx(distance,0.5) as median_distance \
      from df group by home_region, {}".format(frequency, frequency))
      return result

    def different_areas_visited(self, time_filter, frequency):
      result = self.df.where(time_filter)\
        .groupby('msisdn', 'home_region', frequency)\
        .agg(F.countDistinct(F.col('region')).alias('distinct_regions_visited'))\
        .groupby('home_region', frequency)\
        .agg(F.avg('distinct_regions_visited').alias('count'))
      return result

    def only_in_one_region(self, time_filter, frequency):
      result = self.df.where(time_filter)\
        .groupby('msisdn', 'home_region', frequency)\
        .agg(F.countDistinct('region').alias('region_count'))\
        .where(F.col('region_count') == 1)\
        .groupby('home_region', frequency)\
        .agg(F.countDistinct('msisdn').alias('count'))
      return result

    def new_sim(self, time_filter, frequency):
      assert frequency == 'day', 'This indicator is only defined for daily frequency'
      region_month_window = Window.orderBy(F.col('frequency_sec'))\
        .partitionBy('region')\
        .rangeBetween(-days(28), Window.currentRow)
      window_into_the_past = Window.orderBy(F.col('frequency_sec'))\
        .partitionBy('msisdn')\
        .rangeBetween(Window.unboundedPreceding, Window.currentRow)
      result = self.df.where(time_filter)\
        .orderBy(F.col(frequency))\
        .withColumn('frequency_sec', F.col(frequency).cast("long"))\
        .withColumn('new_sim',
        F.when(F.count('msisdn').over(window_into_the_past) == 1, 1).otherwise(0))\
        .groupby('region', frequency, 'frequency_sec')\
        .agg(F.sum('new_sim').alias('new_sims'))\
        .withColumn('new_sims_month', F.sum('new_sims').over(region_month_window))\
        .drop('frequency_sec')
      return result

    def accumulated_incidence(self,
                              time_filter,
                              incubation_period_end = dt.datetime(2020,3,30),
                              incubation_period_start = dt.datetime(2020,3,8),
                              **kwargs):
      user_window_incidence = Window\
        .partitionBy('msisdn').orderBy('stop_number')
      user_window_incidence_rev = Window\
        .partitionBy('msisdn').orderBy(F.desc_nulls_last('stop_number'))
      result = self.df\
        .withColumn('call_datetime_lag',
            F.when(F.col('call_datetime_lag').isNull(),
            self.dates['start']).otherwise(F.col('call_datetime_lag')))\
        .withColumn('call_datetime_lead',
            F.when(F.col('call_datetime_lead').isNull(),
            self.dates['end_date']).otherwise(F.col('call_datetime_lead')))\
        .withColumn('duration',
            (F.col('call_datetime_lead').cast('long') - \
             F.col('call_datetime').cast('long')))\
        .withColumn('stop_number',
            F.row_number().over(user_window_incidence))\
        .where((F.col('day') < incubation_period_end) & \
            (F.col('day') > incubation_period_start))\
        .groupby('msisdn', 'day', 'region')\
        .agg(F.sum('duration').alias('total_duration'),
             F.max('stop_number').alias('stop_number'))\
        .join(self.incidence, 'region', 'left')\
        .withColumn('accumulated_incidence',
            F.col('incidence') * F.col('total_duration') / (21 * 24 * 60 * 60))\
        .withColumn('last_stop',
            F.when(F.col('stop_number') == \
            F.max('stop_number').over(user_window_incidence_rev),
            1).otherwise(0))\
        .withColumn('imported_incidence',
                    F.when(F.col('last_stop') == 1,
                    F.sum(F.col('accumulated_incidence'))\
                    .over(user_window_incidence)).otherwise(0))\
        .groupby('region')\
        .agg(F.sum('imported_incidence').alias('imported_incidence'))
      return result

    def accumulated_incidence_imported_only(self,
                                            time_filter,
                                            incubation_period_end = \
                                            dt.datetime(2020,3,30),
                                            incubation_period_start =\
                                            dt.datetime(2020,3,8),
                                            **kwargs):
      user_window_prep = Window\
        .partitionBy('msisdn').orderBy('call_datetime')
      user_window_incidence = Window\
        .partitionBy('msisdn').orderBy('stop_number')
      user_window_incidence_rev = Window\
        .partitionBy('msisdn').orderBy(F.desc_nulls_last('stop_number'))
      result = self.df.orderBy('call_datetime')\
        .withColumn('call_datetime_lead',
            F.when(F.col('call_datetime_lead').isNull(),
            self.dates['end_date']).otherwise(F.col('call_datetime_lead')))\
        .withColumn('duration',
            (F.col('call_datetime_lead').cast('long') - \
            F.col('call_datetime').cast('long')))\
        .withColumn('stop_number', F.row_number().over(user_window_prep))\
        .where((F.col('day') < incubation_period_end) & \
            (F.col('day') > incubation_period_start))\
        .groupby('msisdn', 'day', 'region')\
        .agg(F.sum('duration').alias('total_duration'),
             F.max('stop_number').alias('stop_number'))\
        .join(self.incidence, 'region', 'left')\
        .withColumn('accumulated_incidence',
            F.col('incidence') * F.col('total_duration') / (21 * 24 * 60 * 60))\
        .withColumn('last_stop',
            F.when(F.col('stop_number') == F.max('stop_number')\
            .over(user_window_incidence_rev), 1).otherwise(0))\
        .withColumn('same_region_as_last_stop',
            F.when((F.col('last_stop') == 0) & (F.col('region') == \
            F.first('region').over(user_window_incidence_rev)), 1).otherwise(0))\
        .withColumn('stop_number_filtered',
            F.row_number().over(user_window_incidence))\
        .withColumn('stop_number_filtered_rev',
            F.row_number().over(user_window_incidence_rev))\
        .withColumn('same_region_as_last_stop_without_break',
            F.when(
            F.sum('same_region_as_last_stop')\
            .over(user_window_incidence_rev) == \
            F.col('stop_number_filtered_rev') - 1,1).otherwise(0))\
        .withColumn('same_region_as_last_stop_with_break',
            F.when((F.col('same_region_as_last_stop') == 1) & \
            (F.col('same_region_as_last_stop_without_break') == 0),
            1).otherwise(0))\
        .withColumn('cutoff',
            F.sum('same_region_as_last_stop_with_break')\
            .over(user_window_incidence_rev))\
        .withColumn('cutoff_indicator',
            F.when((F.col('cutoff') == 0) &\
            (F.sum('same_region_as_last_stop_without_break')\
            .over(user_window_incidence) < \
            F.max('stop_number_filtered')\
            .over(user_window_incidence)), 1).otherwise(0))\
        .withColumn('accumulated_incidence_cutoff',
            F.when((F.col('cutoff_indicator') == 1) & \
            (F.col('same_region_as_last_stop_without_break') == 0),
            F.col('accumulated_incidence')).otherwise(0))\
        .withColumn('imported_incidence',
                    F.when(F.col('last_stop') == 1,
                    F.sum(F.col('accumulated_incidence_cutoff'))\
                    .over(user_window_incidence)).otherwise(0))\
        .groupby('region')\
        .agg(F.sum('imported_incidence').alias('imported_incidence'))
      return result

    def origin_destination_matrix_time_longest_only(self,
                                                    time_filter,
                                                    frequency):
      user_frequency_window = Window\
                                .partitionBy('msisdn', frequency)\
                                .orderBy('call_datetime')
      result = self.df.where(time_filter)\
        .where((F.col('region_lag') != F.col('region')) | \
        (F.col('region_lead') != F.col('region')))\
        .withColumn('duration_lead',
            (F.col('call_datetime_lead').cast('long') - \
            F.col('call_datetime').cast('long')))\
        .withColumn('duration', F.col('duration_lead'))\
        .withColumn('duration_next', F.lead('duration').over(user_frequency_window))\
        .withColumn('duration_change_only',
            F.when(F.col('region') == F.col('region_lead'),
            F.col('duration_next') + F.col('duration'))\
            .otherwise(F.col('duration')))\
        .where(F.col('region_lag') != F.col('region'))\
        .withColumn('max_duration',
            F.when(F.col('duration_change_only') == \
            F.max(F.col('duration_change_only'))\
            .over(user_frequency_window), 1).otherwise(0))\
        .where(F.col('max_duration') == 1)\
        .groupby(frequency, 'region', 'region_lag')\
        .agg(F.sum(F.col('duration_change_only')).alias('total_duration'),
           F.avg(F.col('duration_change_only')).alias('avg_duration'),
           F.count(F.col('duration_change_only')).alias('count'),
           F.stddev_pop(F.col('duration_change_only')).alias('stddev_duration'))
      return result

    def active_residents_from_specific_period(self,
                                              time_filter,
                                              frequency,
                                              exlusion_start = \
                                              dt.datetime(2020,3,1),
                                              active_only_at_home = True):
        user_window = Window.partitionBy('msisdn').orderBy('call_datetime')
        exclusion_filter = (F.col('call_datetime') >= self.dates['start_date']) &\
                           (F.col('call_datetime') < exlusion_start)
        home_locations = self.assign_home_locations(exclusion_filter, 'constant')\
          .withColumnRenamed('msisdn', 'msisdn2')
        home_location_count = home_locations\
          .groupby('home_region')\
          .agg(F.countDistinct('msisdn2').alias('home_location_count'))\
          .withColumnRenamed('home_region', 'home_region2')
        prep = self.df.where(time_filter)\
          .withColumn('first_observation', F.first('call_datetime').over(user_window))\
          .where(F.col('first_observation') < exlusion_start)\
          .drop('home_region')
        prep = prep\
          .join(home_locations, prep.msisdn == home_locations.msisdn2, 'left')
        if active_only_at_home:
            prep = prep.where(F.col('region') == F.col('home_region'))
        prep = prep\
          .groupby('home_region', frequency)\
          .agg(F.countDistinct('msisdn').alias('count'))
        result = prep\
          .join(home_location_count, prep.home_region == \
            home_location_count.home_region2, 'left')\
          .withColumn('percent_active', F.col('count') / F.col('home_location_count'))\
          .drop('home_region2')
        return result


    # - filter for observations that imply a change in region with the previous,
    # observation. Also keep cases where the previous region is unknown, which
    # can be the case for the beginning of the sample
    # - replace missing lead timestamps with the last day of the sample
    # - get the lead duration
    # - create long versions of the timestamp and lead timestamp
    # - calculate duration
    #
    # Now we have duration in each region
    # - Merge with incidence using region and incidence frequency
    #
  
    # - create an incidence sum going back 10 days
    # - collect regions, incidence, durations and lead timestamps in arrays
    # - calculate the beginning timestamps of the infection windows
    # - calculate the size of the windows
    # - repeat the beggining timestamp in an array the length of the window size
    # - zip the beginning timestamps and the 'departure timestamps' arrays and
    # calculate the time that the observation is apart from the beginning timestamps
    # - when the duration from beginning to departure timestamps is smaller than the
    # duration, replace the duration with this difference. This ensures we are not including
    # durations that fall outside of the infectious window into our calculation
    # - zip the incidence and duration arrays and multiply them to get an incidence_duration array
    # - merge the incidence_duration array and the region array,
    # then filter elements where the region == current region
    # - sum imported incidence over infectious period
    #
    # If we want all incidence in one day:
    # - multiply imported incidence by duration in region
    # - group by day and region and sum
    #
    # If we want incidence spread out:
    # - calculate the number of days a person stays in region / number of new rows needed
    # - calculate the remainder time for the last day
    # - make an array to fill the duration column
    # - explode the array into number of rows needed
    # - add a number of day to the date for each row that was exploded
    # - for the last of the exloded rows per user, replace duration with remainder
    # - calculate total incidence imported for the day by duration * incidence (implorted_incidence_0 
    #   is applied to each of the 10 days)
    # - for the above, use the variable depending on how far into our stay we are
    # - group by day and region and sum

    def accumulated_cholera_incidence_imported_only(self,
                                                    time_filter,
                                                    frequency,
                                                    incidence_frequency,
                                                    start_infectious_window = -(10 * 24 * 60 * 60),
                                                    import_in_one_day = True,
                                                    **kwargs):

      user_window = Window\
        .partitionBy('msisdn').orderBy('call_datetime')

      prep = self.df.where(time_filter)\
        .where((F.col('region_lag') != F.col('region')) |\
            (F.col('region_lag') == self.missing_value_code))\
        .withColumn('call_datetime_lead', F.lead('call_datetime').over(user_window))\
        .withColumn('call_datetime_lead',
            F.when(F.col('call_datetime_lead').isNull(),
            self.dates['end_date'] + dt.timedelta(1)).otherwise(F.col('call_datetime_lead')))\
        .withColumn('call_datetime_long', F.col('call_datetime').cast('long'))\
        .withColumn('call_datetime_lead_long', F.col('call_datetime_lead').cast('long'))\
        .withColumn('duration', F.col('call_datetime_lead_long') - F.col('call_datetime_long'))

      if incidence_frequency == 'monthly':
        self.incidence = getattr(self.datasource, 'admin3_cholera_incidence_monthly')
        join_condition = ((prep.region == self.incidence.ward) &\
                          (prep.month == self.incidence.case_month))
      elif incidence_frequency == 'weekly':
        self.incidence = getattr(self.datasource, 'admin3_cholera_incidence_weekly')
        join_condition = ((prep.region == self.incidence.ward) &\
                          (prep.week == self.incidence.case_week))

      result = prep\
        .join(self.incidence, join_condition, 'left')\
        .na.fill({'incidence' : 0})\
        .withColumn('incidence_lag' ,F.lag("incidence", 1, 0).over(user_window))\
        .withColumn('duration_lag' ,F.lag("duration", 1, 0).over(user_window))\
        .withColumn('duration_lag_corrected' ,F.when(F.col('duration_lag') > 864000, 864000)\
                    .otherwise(F.col('duration_lag')))\
        .withColumn('imported_incidence_0', F.col('incidence_lag') * F.col('duration_lag_corrected'))
    
#       user_infection_pickup_window = Window\
#            .partitionBy('msisdn').orderBy('call_datetime_lead_long')\
#            .rangeBetween(Window.unboundedPreceding, 0)

#       result = result\
#          .withColumn('incidence_list',
#              F.collect_list('incidence').over(user_infection_pickup_window))\
#          .withColumn('duration_list',
#              F.collect_list('duration').over(user_infection_pickup_window))\
#          .withColumn('departure_list',
#              F.collect_list('call_datetime_lead_long').over(user_infection_pickup_window))\
#          .withColumn('region_list',
#              F.collect_list('region').over(user_infection_pickup_window))\
#          .withColumn('window_start', F.col('call_datetime_long') + start_infectious_window)\
#          .withColumn('window_size', F.size('departure_list'))\
#          .withColumn('window_start_list', F.expr('array_repeat(window_start, window_size)'))\
#          .withColumn('full_zip', F.arrays_zip(F.col('incidence_list'),
#             F.col('duration_list'), F.col('departure_list'),
#             F.col('region_list'), F.col('window_start_list')))\
#          .withColumn('fitlered_full_zip', F.expr("filter(full_zip, x -> x['departure_list'] > window_start)"))\
#          .withColumn('incidence_list', F.col("fitlered_full_zip").getField('incidence_list'))\
#          .withColumn('duration_list', F.col("fitlered_full_zip").getField('duration_list'))\
#          .withColumn('departure_list', F.col("fitlered_full_zip").getField('departure_list'))\
#          .withColumn('region_list', F.col("fitlered_full_zip").getField('region_list'))\
#          .withColumn('window_start_list', F.col("fitlered_full_zip").getField('window_start_list'))\
#          .withColumn('duration_list_from_window_start',
#              F.expr("transform(arrays_zip(departure_list, window_start_list), x -> x.departure_list - x.window_start_list)"))\
#          .withColumn('duration_corrected_list',
#              F.expr("transform(arrays_zip(duration_list_from_window_start, duration_list), x -> case when x.duration_list_from_window_start > x.duration_list then x.duration_list else x.duration_list_from_window_start end)"))\
#          .withColumn('incidence_duration_list',
#              F.expr("transform(arrays_zip(duration_corrected_list, incidence_list), x -> x.duration_corrected_list * x.incidence_list)"))\
#          .withColumn('zip', F.arrays_zip(F.col('region_list'), F.col('incidence_duration_list')))\
#          .withColumn('filtered_zip', F.expr("filter(zip, x -> x['region_list'] != region)"))\
#          .withColumn('filtered_incidence', F.col("filtered_zip").getField('incidence_duration_list'))\
#          .withColumn('imported_incidence_0', F.expr('AGGREGATE(filtered_incidence, DOUBLE(0), (acc, x) -> acc + x)'))

      if import_in_one_day:
        result = result\
          .withColumn('imported_incidence_time',
               F.col('imported_incidence_0'))\
          .groupby('day', 'region')\
          .agg(F.sum('imported_incidence_time').alias('imported_incidence'))

      else:
        result = result\
          .withColumn('number_of_new_rows',
              F.ceil(F.col('duration') / (24 * 60 * 60)).astype('int'))\
          .withColumn('remainder',
              F.col('number_of_new_rows') * (24 * 60 * 60) - F.col('duration'))\
          .withColumn('number_of_new_rows',
              F.when(F.col('number_of_new_rows') > 10, 10)\
              .otherwise(F.col('number_of_new_rows')))\
          .withColumn('new_row_array',
              F.when(F.col('number_of_new_rows')>1,
              F.expr('array_repeat(24 * 60 * 60,number_of_new_rows)'))\
              .otherwise(F.array('duration')))\
          .selectExpr('*',
              "posexplode(new_row_array) as (pos, duration_exploded)",
              "date_add(day, pos) as day_filled")\
          .withColumn('pos_lead', F.lead('pos').over(user_window))\
          .withColumn('duration_exact',
              F.when(F.col('pos') > F.col('pos_lead'), F.col('remainder'))\
              .otherwise(F.col('duration_exploded')))\
          .withColumn('duration_day_fraction', F.col('duration_exact') / (24 * 60 * 60))\
          .withColumn('imported_incidence_time',
                F.when(F.col('pos') == 0,
                F.col('imported_incidence_0') * \
                F.col('duration_day_fraction')).otherwise(
                F.when(F.col('pos') == 1,
                F.col('imported_incidence_0') * \
                F.col('duration_day_fraction')).otherwise(
                F.when(F.col('pos') == 2,
                F.col('imported_incidence_0') * \
                F.col('duration_day_fraction')).otherwise(
                F.when(F.col('pos') == 3,
                F.col('imported_incidence_0') * \
                F.col('duration_day_fraction')).otherwise(
                F.when(F.col('pos') == 4,
                F.col('imported_incidence_0') * \
                F.col('duration_day_fraction')).otherwise(
                F.when(F.col('pos') == 5,
                F.col('imported_incidence_0') * \
                F.col('duration_day_fraction')).otherwise(
                F.when(F.col('pos') == 6,
                F.col('imported_incidence_0') * \
                F.col('duration_day_fraction')).otherwise(
                F.when(F.col('pos') == 7,
                F.col('imported_incidence_0') * \
                F.col('duration_day_fraction')).otherwise(
                F.when(F.col('pos') == 8,
                F.col('imported_incidence_0') * \
                F.col('duration_day_fraction')).otherwise(
                F.when(F.col('pos') == 9,
                F.col('imported_incidence_0') * \
                F.col('duration_day_fraction')).otherwise(0)))))))))))\
           .groupby('day_filled', 'region')\
           .agg(F.sum('imported_incidence_time').alias('imported_incidence'))\
           .withColumnRenamed('day_filled', 'day')

      return result