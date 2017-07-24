# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import math
import time
from datetime import datetime, timedelta
from geopy.distance import great_circle

from pokemongo_bot.base_task import BaseTask
from pokemongo_bot.cell_workers.utils import coord2merc, merc2coord
from pokemongo_bot.constants import Constants
from pokemongo_bot.walkers.polyline_walker import PolylineWalker
from pokemongo_bot.walkers.step_walker import StepWalker
from pokemongo_bot.worker_result import WorkerResult
from pokemongo_bot.item_list import Item
from pokemongo_bot import inventory
from .utils import distance, format_dist, fort_details

LOG_TIME_INTERVAL = 30
NO_BALLS_MOVING_TIME = 5 * 60

# Okay, we have a shortage of Pokeballs and we need to fill the back with them
# The idea is to move to the biggest cluster of Pokestops/Gyms to fille the bag
# So we look for the biggest cluster, move there and spin the stops
# When we have spinned those stops, we look around again to find the biggest cluster
# of unspinned Pokestops/gyms and move there, and so on!

class BallCollector(BaseTask):
  SUPPORTED_TASK_API_VERSION = 1

  def __init__(self, bot, config):
    super(BallCollector, self).__init__(bot, config)

  def initialize(self):
    self.clusters = None
    self.config_max_distance = self.config.get("max_distance", 3000)
    self.min_balls = self.config.get("min_balls",20)
    self.resume_balls = self.config.get("resume_balls",100)
    self.duration = self.config.get("duration",15)
    self.config_min_forts_count = self.config.get("min_forts_count", 3)

    self.no_log_until = datetime.now()
    if not hasattr(self.bot, "catch_resume_at"): self.bot.catch_resume_at = None
    self.clusters = None
    self.cluster = None
    self.walker = None
    self.previous_distance = 0


  def work(self):
    # Don't do anything when softbanned!!!
    if hasattr(self.bot, "softban") and self.bot.softban:
      return WorkerResult.SUCCESS

    balls_on_hand = self.get_pokeball_count()
    now = datetime.now()

    if balls_on_hand > self.resume_balls:
      # self.logger.info("Balls on hand %s more than %s" % (balls_on_hand, self.resume_balls) )
      if self.bot.catch_disabled:
        self.emit_event(
                'catch_limit_off',
                formatted="Balls on hand ({}) exceeds threshold {}. Re-enabling catch tasks.".
                    format(balls_on_hand, self.resume_balls)
            )
        self.bot.catch_disabled = False
      return WorkerResult.SUCCESS

    if self.bot.catch_disabled and now >= self.bot.catch_resume_at:
      if balls_on_hand > self.min_balls:
        self.emit_event(
            'catch_limit_off',
            formatted="Resume time has passed and balls on hand ({}) exceeds threshold {}. Re-enabling catch tasks.".
                format(balls_on_hand,self.min_balls)
        )
        self.bot.catch_disabled = False

    # If balls_on_hand less than threshold, pause catching tasks for duration minutes
    if not self.bot.catch_disabled and balls_on_hand <= self.min_balls:
      # Okay, we need to fillup on balls now, so we notify other workers to disable catching
      self.bot.catch_resume_at = now + timedelta(minutes = self.duration)
      self.no_log_until = now + timedelta(minutes = 2)
      self.bot.catch_disabled = True
      self.emit_event(
          'catch_limit_on',
          formatted="Balls on hand ({}) has reached threshold {}. Disabling catch tasks until {} or balls on hand > threshold (whichever is later).".
              format(balls_on_hand, self.min_balls, self.bot.catch_resume_at.strftime("%H:%M:%S"))
      )
    # Check if we are looking to fillup on balls
    if hasattr(self.bot, "catch_disabled") and not self.bot.catch_disabled:
      # We are not looking to fillup on balls
      return WorkerResult.SUCCESS

    if self.bot.catch_disabled and self.no_log_until <= now:
      if now >= self.bot.catch_resume_at:
        self.logger.info("All catch tasks disabled until balls on hand (%s) > threshold." % balls_on_hand)
      else:
        self.logger.info("All catch tasks disabled until %s or balls on hand (%s) >= %s" % (self.bot.catch_resume_at.strftime("%H:%M:%S"), balls_on_hand, self.resume_balls))

    # Let's get our clusters
    # self.logger.info("Looking for ball cluster...")
    if self.cluster is None:
      forts = self.get_forts()
      self.clusters = self.get_clusters(forts.values())
      # self.logger.info("%s clusters found..." % len(self.clusters))

      available_clusters = self.get_available_clusters()

      if len(available_clusters) > 0:
        self.cluster = available_clusters[0]
        self.walker = PolylineWalker(self.bot, self.cluster["center"][0], self.cluster["center"][1])

        self.no_log_until = now + timedelta(seconds = LOG_TIME_INTERVAL)
        self.emit_event("new_destination",
                        formatted='New destination at {distance:.2f} meters: {size} forts'.format(**self.cluster))
      else:
        # No cluster found to move to...
        self.cluster = None
        self.clusters = None

    if self.cluster is not None:
      # Update the distance to the current cluster
      self.update_cluster_distance(self.cluster)

      # Move to the cluster or arrive
      if self.walker.step():
        self.distance_counter = 0
        self.emit_event("arrived_at_destination",
                        formatted="Arrived at destination: {size} forts.".format(**self.cluster))
        self.cluster = None
        self.previous_distance = 0

      elif self.no_log_until < now:
        if self.previous_distance == self.cluster["distance"]:
          self.distance_counter += 1
          if self.distance_counter == 3:
              self.logger.info("Having difficulty walking to the cluster, changing walker!")
              self.walker = StepWalker(self.bot, self.cluster["center"][0], self.cluster["center"][1])
          elif self.distance_counter > 6:
              self.logger.info("Can't walk to the cluster!")
              self.distance_counter = 0
              self.cluster = None
              self.clusters = None
              return WorkerResult.ERROR
        elif self.distance_counter > 0:
          self.distance_counter -= 1

        self.previous_distance = self.cluster["distance"]

        self.no_log_until = now + timedelta(seconds = LOG_TIME_INTERVAL)
        # self.no_log_until = now + LOG_TIME_INTERVAL
        self.emit_event("moving_to_destination",
                      formatted="Moving to destination at {distance:.2f} meters: {size} forts.".format(**self.cluster))

    if self.cluster is None:
      # get the nearest fort and move there!
      forts = self.bot.get_forts(order_by_distance=True)
      forts = filter(lambda x: x["id"] not in self.bot.fort_timeouts, forts)
      if len(forts) == 0:
        self.logger.info("No forts around?")
        return WorkerResult.SUCCESS

      nearest_fort = forts[0]
      lat = nearest_fort['latitude']
      lng = nearest_fort['longitude']
      fortID = nearest_fort['id']
      details = fort_details(self.bot, fortID, lat, lng)
      fort_name = details.get('name', 'Unknown')

      unit = self.bot.config.distance_unit  # Unit to use when printing formatted distance

      dist = distance(
              self.bot.position[0],
              self.bot.position[1],
              lat,
              lng
          )
      moving = dist > Constants.MAX_DISTANCE_FORT_IS_REACHABLE

      if moving:
        self.walker = StepWalker(self.bot, lat, lng)
        if "type" in nearest_fort and nearest_fort["type"] == 1:
          # It's a Pokestop
          target_type = "pokestop"
        else:
          # It's a gym
          target_type = "gym"
        while not self.walker.step():
          dist = distance(
              self.bot.position[0],
              self.bot.position[1],
              lat,
              lng
          )
          moving = dist > Constants.MAX_DISTANCE_FORT_IS_REACHABLE
          if moving:
            fort_event_data = {
              'fort_name': u"{}".format(fort_name),
              'distance': format_dist(dist, unit),
              'target_type': target_type,
            }
            self.emit_event(
              'moving_to_fort',
              formatted="Moving towards {target_type} {fort_name} - {distance}",
              data=fort_event_data
            )
            return WorkerResult.RUNNING
          else:
            self.emit_event(
              'arrived_at_fort',
              formatted='Arrived at fort %s.' % fort_name
            )

        return WorkerResult.SUCCESS

    self.no_log_until = now + timedelta(seconds = LOG_TIME_INTERVAL)
    return WorkerResult.RUNNING

  def get_pokeball_count(self):
    return sum([inventory.items().get(ball.value).count for ball in [Item.ITEM_POKE_BALL, Item.ITEM_GREAT_BALL, Item.ITEM_ULTRA_BALL]])

  def get_forts(self):
    radius = self.config_max_distance + Constants.MAX_DISTANCE_FORT_IS_REACHABLE

    # Get all the Pokestops and Gyms in range that are not on cooldown
    forts = [f for f in self.bot.cell["forts"] if f["id"] not in self.bot.fort_timeouts]
    # Filter out those not in range
    forts = [f for f in forts if self.get_distance(self.bot.start_position, f) <= radius]

    return {f["id"]: f for f in forts}

  def get_available_clusters(self):
    for cluster in self.clusters:
      self.update_cluster_distance(cluster)

    # available_clusters = [c for c in self.clusters if c["lured"] >= self.config_min_lured_forts_count]
    available_clusters = [c for c in self.clusters if c["size"] >= self.config_min_forts_count]
    available_clusters.sort(key=lambda c: self.get_cluster_key(c), reverse=True)

    return available_clusters

  def get_clusters(self, forts):
    clusters = []
    points = self.get_all_snap_points(forts)

    for c1, c2, fort1, fort2 in points:
      cluster_1 = self.get_cluster(forts, c1)
      cluster_2 = self.get_cluster(forts, c2)

      self.update_cluster_distance(cluster_1)
      self.update_cluster_distance(cluster_2)

      key_1 = self.get_cluster_key(cluster_1)
      key_2 = self.get_cluster_key(cluster_2)

      radius = Constants.MAX_DISTANCE_FORT_IS_REACHABLE

      if key_1 >= key_2:
        cluster = cluster_1

        while True:
          new_circle, _ = self.get_enclosing_circles(fort1, fort2, radius - 1)

          if not new_circle:
            break

            new_cluster = self.get_cluster(cluster["forts"], new_circle)

            if len(new_cluster["forts"]) < len(cluster["forts"]):
              break

            cluster = new_cluster
            radius -= 1
          else:
            cluster = cluster_2

            while True:
              _, new_circle = self.get_enclosing_circles(fort1, fort2, radius - 1)

              if not new_circle:
                break

              new_cluster = self.get_cluster(cluster["forts"], new_circle)

              if len(new_cluster["forts"]) < len(cluster["forts"]):
                break

              cluster = new_cluster
              radius -= 1

          clusters.append(cluster)
    return clusters

  def get_all_snap_points(self, forts):
    points = []
    radius = Constants.MAX_DISTANCE_FORT_IS_REACHABLE

    for i in range(0, len(forts)):
      for j in range(i + 1, len(forts)):
        c1, c2 = self.get_enclosing_circles(forts[i], forts[j], radius)

        if c1 and c2:
          points.append((c1, c2, forts[i], forts[j]))

    return points

  def get_enclosing_circles(self, fort1, fort2, radius):
    x1, y1 = coord2merc(fort1["latitude"], fort1["longitude"])
    x2, y2 = coord2merc(fort2["latitude"], fort2["longitude"])
    dx = x2 - x1
    dy = y2 - y1
    d = math.sqrt(dx ** 2 + dy ** 2)

    if (d == 0) or (d > 2 * radius):
      return None, None

    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    cd = math.sqrt(radius ** 2 - (d / 2) ** 2)

    c1 = merc2coord((cx - cd * dy / d, cy + cd * dx / d)) + (radius,)
    c2 = merc2coord((cx + cd * dy / d, cy - cd * dx / d)) + (radius,)

    return c1, c2

  def get_cluster(self, forts, circle):
    forts_in_circle = [f for f in forts if self.get_distance(circle, f) <= circle[2]]

    cluster = {"center": (circle[0], circle[1]),
               "distance": 0,
               "forts": forts_in_circle,
               "size": len(forts_in_circle)}

    return cluster

  def get_cluster_key(self, cluster):
    return (cluster["size"], -cluster["distance"])

  def update_cluster_distance(self, cluster):
    cluster["distance"] = great_circle(self.bot.position, cluster["center"]).meters

  def get_distance(self, location, fort):
    return great_circle(location, (fort["latitude"], fort["longitude"])).meters
