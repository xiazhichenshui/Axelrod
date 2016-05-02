from __future__ import absolute_import

from collections import defaultdict
import logging
from multiprocess import Process, Queue, cpu_count
import csv

from .game import Game
from .result_set import ResultSet
from .match_generator import RoundRobinMatches, ProbEndRoundRobinMatches
from .match import Match

class Tournament(object):
    game = Game()

    def __init__(self, players, match_generator=RoundRobinMatches,
                 name='axelrod', game=None, turns=200, repetitions=10,
                 processes=None, chunk_size=100, noise=0, with_morality=True):
        """
        Parameters
        ----------
        players : list
            A list of axelrod.Player objects
        match_generator : class
            A class that must be descended from axelrod.MatchGenerator
        name : string
            A name for the tournament
        game : axelrod.Game
            The game object used to score the tournament
        turns : integer
            The number of turns per match
        repetitions : integer
            The number of times the round robin should be repeated
        processes : integer
            The number of processes to be used for parallel processing
        chunk_size : integer the size of the chunks of matches to be generated.
            Mainly relevant for large tournaments and parallel processing.
        noise : float
            The probability that a player's intended action should be flipped
        with_morality : boolean
            Whether morality metrics should be calculated
        """
        self.name = name
        self.turns = turns
        self.noise = noise
        if game is not None:
            self.game = game
        self.players = players
        self.repetitions = repetitions
        self.match_generator = match_generator(players, turns, self.game,
                                               self.repetitions)
        self._with_morality = with_morality
        self._parallel_repetitions = repetitions
        self._processes = processes
        self._logger = logging.getLogger(__name__)
        self.interactions = defaultdict(list)

    def play(self, filename=None):
        """
        Plays the tournament and passes the results to the ResultSet class

        Returns
        -------
        axelrod.ResultSet
        """
        if self._processes is None:
            self._run_serial(filename)
        else:
            self._run_parallel(filename)

        if filename is None:
            self.result_set = self._build_result_set()
            return self.result_set

    def _build_result_set(self):
        """
        Build the result set (used by the play method)

        Returns
        -------
        axelrod.ResultSet
        """
        result_set = ResultSet(
            players=self.players,
            interactions=self.interactions,
            with_morality=self._with_morality)
        return result_set

    def _run_serial(self, filename=None):
        """
        Runs all repetitions of the round robin in serial.

        Parameters
        ----------
        ineractions : list
            The list of interactions per repetition to update with results
        """
        chunks = self.match_generator.build_match_chunks()

        for chunk in chunks:
            interactions = self._play_matches(chunk)
            self._write_interactions(filename, interactions)
        return True

    def _write_interactions(self, filename, interactions):
        """Either write to memory or to file"""
        if filename:
            self._write_to_csv(filename, interactions)
        else:
            self._write_to_memory(interactions)

    def _write_to_memory(self, interactions):
        """Write the given interactions to the interactions attribute"""
        for index_pair, interactions in interactions.items():
            self.interactions[index_pair].extend(interactions)

    def _write_to_csv(self, filename, results):
        """Write the interactions to csv."""
        f = lambda x: "".join(x)
        with open(filename, 'a') as csvfile:
            writer = csv.writer(csvfile)
            for index_pair, interactions in results.items():
                for interaction in interactions:
                    row = list(index_pair)
                    row.append(str(self.players[index_pair[0]]))
                    row.append(str(self.players[index_pair[1]]))
                    row.append(f(map(f, interaction)))
                    writer.writerow(row)

    def _run_parallel(self, filename):
        """
        Run all except the first round robin using parallel processing.

        Parameters
        ----------
        interactions : list
            The list of interactions per repetition to update with results
        """
        # At first sight, it might seem simpler to use the multiprocessing Pool
        # Class rather than Processes and Queues. However, Pool can only accept
        # target functions which can be pickled and instance methods cannot.
        work_queue = Queue()
        done_queue = Queue()
        workers = self._n_workers()

        chunks = self.match_generator.build_match_chunks()
        for chunk in chunks:
            # This is crap: I'm going through the generator, ideally want the
            # chunk to be a generator also
            work_queue.put(chunk)

        self._start_workers(workers, work_queue, done_queue)
        self._process_done_queue(workers, done_queue, filename)

        return True

    def _n_workers(self):
        """
        Determines the number of parallel processes to use.

        Returns
        -------
        integer
        """
        if (2 <= self._processes <= cpu_count()):
            n_workers = self._processes
        else:
            n_workers = cpu_count()
        return n_workers

    def _start_workers(self, workers, work_queue, done_queue):
        """
        Initiates the sub-processes to carry out parallel processing.

        Parameters
        ----------
        workers : integer
            The number of sub-processes to create
        work_queue : multiprocessing.Queue
            A queue containing an entry for each round robin to be processed
        done_queue : multiprocessing.Queue
            A queue containing the output dictionaries from each round robin
        """
        for worker in range(workers):
            process = Process(
                target=self._worker, args=(work_queue, done_queue))
            work_queue.put('STOP')
            process.start()
        return True

    def _process_done_queue(self, workers, done_queue, filename):
        """
        Retrieves the matches from the parallel sub-processes

        Parameters
        ----------
        workers : integer
            The number of sub-processes in existence
        done_queue : multiprocessing.Queue
            A queue containing the output dictionaries from each round robin
        interactions : list
            The list of interactions per repetition to update with results
        """
        stops = 0
        while stops < workers:
            results = done_queue.get()

            if results == 'STOP':
                stops += 1
            else:
                self._write_interactions(filename, results)
        return True

    def _worker(self, work_queue, done_queue):
        """
        The work for each parallel sub-process to execute.

        Parameters
        ----------
        work_queue : multiprocessing.Queue
            A queue containing an entry for each round robin to be processed
        done_queue : multiprocessing.Queue
            A queue containing the output dictionaries from each round robin
        """
        for chunk in iter(work_queue.get, 'STOP'):
            interactions = self._play_matches(chunk)
            done_queue.put(interactions)
        done_queue.put('STOP')
        return True

    def _play_matches(self, chunk):
        """
        Play the supplied matches.

        Parameters
        ----------
        matches : generator
            Generator of tuples: player index pair, match

        Returns
        -------
        interactions : dictionary
            Mapping player index pairs to results of matches:

                (0, 1) -> [('C', 'D'), ('D', 'C'),...]
        """
        interactions = defaultdict(list)
        index_pair, match_params, repetitions = chunk
        p1_index, p2_index = index_pair
        player1 = self.players[p1_index].clone()
        player2 = self.players[p2_index].clone()
        players = (player1, player2)
        params = [players]
        params.extend(match_params)
        match = Match(*params)
        for _ in range(repetitions):
            match.play()
            interactions[index_pair].append(match.result)
        return interactions


class ProbEndTournament(Tournament):
    """
    A tournament in which the player don't know the length of a given match
    (currently implemented by setting this to be infinite). The length of a
    match is equivalent to randomly sampling after each round whether or not to
    continue.
    """

    def __init__(self, players, match_generator=ProbEndRoundRobinMatches,
                 name='axelrod', game=None, prob_end=.5, repetitions=10,
                 processes=None,
                 noise=0,
                 with_morality=True):
        """
        Parameters
        ----------
        players : list
            A list of axelrod.Player objects
        match_generator : class
            A class that must be descended from axelrod.MatchGenerator
        name : string
            A name for the tournament
        game : axelrod.Game
            The game object used to score the tournament
        prob_end : a float
            The probability of a given match ending
        repetitions : integer
            The number of times the round robin should be repeated
        processes : integer
            The number of processes to be used for parallel processing
        noise : float
            The probability that a player's intended action should be flipped
        with_morality : boolean
            Whether morality metrics should be calculated
        """
        super(ProbEndTournament, self).__init__(
            players, name=name, game=game, turns=float("inf"),
            repetitions=repetitions, processes=processes,
            noise=noise, with_morality=with_morality)

        self.prob_end = prob_end
        self.match_generator = ProbEndRoundRobinMatches(
            players, prob_end, self.game, repetitions)
