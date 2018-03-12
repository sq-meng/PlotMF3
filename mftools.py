import multiprocessing as mp
import itertools
import numpy as np
import pandas as pd
import re
from collections import defaultdict
import voronoi_plot
import ubtools
from ubtools import UBMatrix, NUM_CHANNELS, CHANNEL_SEPARATION, EF_LIST, etok, ktoe, calculate_locus, angle_to_qs
import pyclipper
import matplotlib.pyplot as plt
import matplotlib.patches as mpl_patches
import matplotlib.path as mpl_path
import tkinter
from tkinter import filedialog
import os

try:
    DETECTOR_WORKING = np.loadtxt('res/alive.csv')
except FileNotFoundError:
    print('Dead detector map not found - assuming all working.')
    DETECTOR_WORKING = np.ones(NUM_CHANNELS, len(EF_LIST))

try:
    WEIGHTS = np.loadtxt('res/weights.csv', delimiter=',')
except FileNotFoundError:
    print('Boundary angle channel strategy not defined - assuming equal weights.')
    WEIGHTS = np.ones(NUM_CHANNELS, len(EF_LIST))

try:
    INTENSITY_COEFFICIENT = np.loadtxt('res/int_corr.csv', delimiter=',')
except FileNotFoundError:
    print('Intensity correction matrix not found - assuming all ones.')
    INTENSITY_COEFFICIENT = np.ones(NUM_CHANNELS, 1)


def nan_float(string):
    try:
        return float(string)
    except ValueError:
        if '*' in string:
            return np.NaN
        else:
            raise


def nan_int(string):
    try:
        return int(string)
    except ValueError:
        if '*' in string:
            return np.NaN
        else:
            raise


def _parse_flatcone_line(line):
    data = np.array([nan_int(x) for x in line.split()])
    array = np.reshape(data, (-1, len(EF_LIST)))[0: -1, :]  # throws out last line which is only artifact
    ang_channels = np.array([np.arange(1, NUM_CHANNELS + 1)]).T  # starts at 1 to match stickers
    array_with_ch_no = np.hstack([ang_channels, array])
    dataframe_flatcone = pd.DataFrame(data=array_with_ch_no, columns=['aCh', 'e1', 'e2', 'e3', 'e4', 'e5'])
    dataframe_flatcone.set_index('aCh', inplace=True)
    return dataframe_flatcone


def _parse_param_line(line):
    line_name = line[0:5]
    line_body = line[6:].strip()
    if line_name == 'COMND':
        no_points = int(re.findall('(?<=NP)[\s\t0-9]*', line_body)[0].strip())
        return line_name, {'value': line_body, 'NP': no_points}
    elif '=' not in line_body:
        return line_name, line_body
    else:
        equations = line_body.split(',')
        line_dict = {}
        for eq in equations:
            param_name, value_raw = [x.strip() for x in eq.split('=')]
            try:
                value = nan_float(value_raw)
            except ValueError:
                value = value_raw
            line_dict[param_name] = value
        return line_name, line_dict


def parse_ill_data(file_object, start_flag='DATA_:\n'):
    # first parse headers
    try:
        file_object.seek(0, 0)
    except AttributeError:
        file_object = open(file_object, 'r')
    text_data = file_object.read()
    headers = re.findall('^[A-Z_]{5}:.*', text_data, re.MULTILINE)
    header_dict = defaultdict(dict)
    for line in headers:
        line_name, line_body = _parse_param_line(line)
        if type(line_body) is dict:
            header_dict[line_name].update(line_body)
        else:
            header_dict[line_name].update({'value': line_body})
    # then parse scan parameters and counts
    data_section = text_data[text_data.find(start_flag) + len(start_flag) + 1:]
    column_names = data_section.splitlines()[0].split()
    # line only w 0-9, . -, spc, tab
    parameters_text_lines = re.findall('^[0-9*\-\s\t.]+?$', data_section, re.MULTILINE)
    parameters_value_array = np.array([[nan_float(num) for num in line.split()] for line in parameters_text_lines])
    data_frame = pd.DataFrame(data=parameters_value_array, columns=column_names)
    data_frame['PNT'] = data_frame['PNT'].astype('int16')
    df_clean = data_frame.T.drop_duplicates().T
    # parse flatcone data if present
    flat_all = re.findall('(?<=flat: )[0-9w\s\t\n*]+(?=endflat)', text_data, re.MULTILINE)
    flat_number_lines = len(flat_all)
    if len(df_clean) == 0:
        raise ValueError('file %s does contain any data.' % file_object.name)
    if len(df_clean) - flat_number_lines <= 1:  # sanity check: only 1 missing flatcone line is acceptable
        flat_frames = []
        for nth, line in enumerate(flat_all):
            try:
                flat_frames.append(_parse_flatcone_line(line))
            except ValueError:
                raise ValueError('point %d in file %s is faulty.' % (nth + 1, file_object.name))
        if len(df_clean) - flat_number_lines == 1:
            df_clean.drop(df_clean.index[-1], inplace=True)  # if only one line is missing then just drop last line
        df_clean = df_clean.assign(flat=flat_frames)
    else:
        pass
    return dict(header_dict), df_clean


def ub_from_header(header_dict):
    param = header_dict['PARAM']
    lattice_parameters = [param['AS'], param['BS'], param['CS'], param['AA'], param['BB'], param['CC']]
    hkl1 = [float(param['AX']), float(param['AY']), float(param['AZ'])]
    hkl2 = [float(param['BX']), float(param['BY']), float(param['BZ'])]
    ub_matrix = UBMatrix(lattice_parameters, hkl1, hkl2)
    return ub_matrix


class Scan(object):
    def __init__(self, file_name, ub_matrix=None, intensity_matrix=None):
        f = open(file_name)
        self.header, self.data = parse_ill_data(f)
        self.file_name = file_name
        if 'flat' not in self.data.columns:
            raise AttributeError('%s does not contain flatcone data.' % file_name)
        elif 'A3' not in self.header['STEPS'].keys():
            raise AttributeError('%s is not A3 scan.' % file_name)

        if intensity_matrix:
            self.intensity_matrix = intensity_matrix
        else:
            self.intensity_matrix = INTENSITY_COEFFICIENT

        if not ub_matrix:
            self.ub_matrix = ub_from_header(self.header)
        else:
            self.ub_matrix = ub_matrix

        try:
            self.ki = self.data.iloc[0]['KI']
        except KeyError:
            try:
                self.ki = etok(self.data.iloc[0]['EI'])
            except KeyError:
                raise KeyError('File %s records neither ki nor Ei.' % self.file_name)

        self.a3_ranges, self.a4_ranges = None, None
        self._update_scan_ranges()
        try:
            self.tt = self.data.iloc[-1]['TT']  # takes final value as signature value for the scan
        except KeyError:
            self.tt = None

        try:
            self.mag = self.data.iloc[-1]['MAG']
        except KeyError:
            self.mag = None

        self.planned_locus_list, self.actual_locus_list = [], []
        self._update_locus()
        self.converted_dataframes = []
        self._populate_data_array()
        print('finished loading %s' % file_name)

    @property
    def ei(self):
        return ktoe(self.ki)

    @property
    def np_planned(self):
        return self.header['COMND']['NP']

    @property
    def np_actual(self):
        return len(self.data)

    def _update_locus(self):
        self.planned_locus_list = []
        self.actual_locus_list = []
        kf_list = [etok(e) for e in EF_LIST]
        a3_start, a3_end_actual, a3_end_planned = self.a3_ranges
        a4_start, a4_end_actual, a4_end_planned = self.a4_ranges
        self.planned_locus_list = [calculate_locus(self.ki, kf, a3_start, a3_end_planned, a4_start, a4_end_planned,
                                                   self.ub_matrix, expand_a3=True) for kf in kf_list]
        self.actual_locus_list = [calculate_locus(self.ki, kf, a3_start, a3_end_actual, a4_start, a4_end_actual,
                                                  self.ub_matrix) for kf in kf_list]

    def _populate_data_array(self):
        num_ch = NUM_CHANNELS
        channel_separation = CHANNEL_SEPARATION
        num_flat_frames = len(self.data)
        # an numpy array caching a3, a4 angles and monitor counts, shared across all energy channels
        a3_a4_mon_array = np.zeros([num_flat_frames * num_ch, 3])

        a4_angle_mask = np.linspace(-channel_separation * (num_ch - 1) / 2,
                                    channel_separation * (num_ch - 1) / 2, num_ch)

        for i in range(num_flat_frames):
            a3_a4_mon_array[i * num_ch: (i + 1) * num_ch, 0] = self.data.loc[i, 'A3']
            a3_a4_mon_array[i * num_ch: (i + 1) * num_ch, 1] = self.data.loc[i, 'A4'] + a4_angle_mask
            a3_a4_mon_array[i * num_ch: (i + 1) * num_ch, 2] = self.data.loc[i, 'M1']

        data_template = pd.DataFrame(index=range(num_flat_frames * num_ch),
                                     columns=['A3', 'A4', 'MON', 'px', 'py', 'pz', 'counts', 'valid', 'coeff',
                                              'ach', 'point'], dtype='float64')
        data_template = data_template.assign(file=pd.Series(index=range(num_flat_frames * num_ch)), dtype='str')
        data_template.loc[:, ['A3', 'A4', 'MON']] = a3_a4_mon_array
        self.converted_dataframes = [data_template.copy() for _ in range(len(EF_LIST))]
        for ef_channel_num, ef in enumerate(EF_LIST):
            self.converted_dataframes[ef_channel_num].loc[:, ['px', 'py', 'pz']] = self.ub_matrix.convert(
                angle_to_qs(self.ki, etok(ef), a3_a4_mon_array[:, 0], a3_a4_mon_array[:, 1]), 'sp'
            ).T
        coefficient = INTENSITY_COEFFICIENT
        detector_working = DETECTOR_WORKING
        for point_num in range(num_flat_frames):
            flatcone_array = np.array(self.data.loc[point_num, 'flat'])
            for ef_channel_num in range(len(EF_LIST)):
                dataframe = self.converted_dataframes[ef_channel_num]
                rows = slice(point_num * num_ch, (point_num + 1) * num_ch - 1, None)
                dataframe.loc[rows, 'counts'] = flatcone_array[:, ef_channel_num]
                dataframe.loc[rows, 'valid'] = detector_working[:, ef_channel_num]
                dataframe.loc[rows, 'coeff'] = coefficient[:, ef_channel_num]
                dataframe.loc[rows, 'point'] = self.data.loc[point_num, 'PNT']
                dataframe.loc[rows, 'ach'] = range(1, num_ch + 1)
                dataframe.loc[rows, 'file'] = self.file_name

    def _update_scan_ranges(self):
        a3_start = self.data.iloc[0]['A3']
        a3_end_actual = self.data.iloc[-1]['A3']
        try:
            a3_end_planned = self.header['VARIA']['A3'] + self.header['STEPS']['A3'] * (self.header['COMND']['NP'] - 1)
        except KeyError:
            a3_end_planned = a3_end_actual

        a4_start = self.header['VARIA']['A4']  # A4 is not necessarily outputted in data
        if 'A4' not in self.header['STEPS']:
            a4_end_planned = a4_start
            a4_end_actual = a4_start
        else:
            a4_end_planned = self.header['VARIA']['A4'] + self.header['STEPS']['A4'] * (self.header['COMND']['NP'] - 1)
            a4_end_actual = self.data.iloc[-1]['A4']

        self.a3_ranges = (a3_start, a3_end_actual, a3_end_planned)
        self.a4_ranges = (a4_start, a4_end_actual, a4_end_planned)

    def to_csv(self, file_name=None, channel=None):
        pass


def chasm_bins(values, tolerance=0.2) -> list:
    """
    :param values: An iterable list of all angles, repetitions allowed.
    :param tolerance: maximum difference in value for considering two points to be the same.
    :return: a list of bin edges

    Walks through sorted unique values, if a point is further than tolerance away from the next, a bin edge is
    dropped between the two points, otherwise no bin edge is added. A beginning and ending edge is added at
    tolerance / 2 further from either end.
    """
    values_array = np.array(values).ravel()
    unique_values = np.array(list(set(values_array)))
    unique_values.sort()
    bin_edges = [unique_values[0] - tolerance / 2]
    for i in range(len(unique_values) - 1):
        if unique_values[i+1] - unique_values[i] > tolerance:
            bin_edges.append((unique_values[i] + unique_values[i+1]) / 2)
        else:
            pass

    bin_edges.append(unique_values[-1] + tolerance / 2)

    return bin_edges


def bin_locus(locus_list):
    clipper = pyclipper.Pyclipper()
    for locus in locus_list:
        clipper.AddPath(pyclipper.scale_to_clipper(locus), pyclipper.PT_SUBJECT)

    merged_locus = np.array(pyclipper.scale_from_clipper(clipper.Execute(pyclipper.CT_UNION, pyclipper.PFT_NONZERO)))
    return merged_locus


def bin_scan_points(data_frames):
    joined_frames = pd.concat(data_frames, axis=0, ignore_index=True)
    joined_frames = joined_frames.assign(counts_norm=joined_frames.counts/joined_frames.coeff)
    joined_frames = joined_frames.drop(joined_frames[joined_frames.valid != 1].index)  # delete dead detectors

    a3_cuts = bin_and_cut(joined_frames.A3)
    a4_cuts = bin_and_cut(joined_frames.A4)
    group = joined_frames.groupby([a3_cuts, a4_cuts])
    sums = group['counts', 'counts_norm', 'MON'].sum()
    means = group['A3', 'A4', 'px', 'py', 'pz'].mean()
    error_bars = np.sqrt(sums.counts)
    per_monitor = sums.counts_norm / sums.MON
    result = pd.concat([sums, means], axis=1)
    result = result.assign(err=error_bars)
    result = result.assign(permon=per_monitor)
    result = result.dropna()
    return result.reset_index(drop=True)


def bin_and_cut(data: pd.Series, tolerance=0.2):
    bin_edges = chasm_bins(data, tolerance)
    cut = pd.cut(data, bin_edges)
    return cut


def series_to_binder(items: pd.Series):
    return DataBinder(list(items))


def bin_scans(list_of_data, nan_fill=0, ignore_ef=False, en_tolerance=0.05, tt_tolerance=0.5, mag_tolerance=0.05):
    df = pd.DataFrame(index=range(len(list_of_data) * len(EF_LIST)),
                      columns=['name', 'ei', 'ef', 'en', 'tt', 'mag', 'points', 'locus_a', 'locus_p'])
    for i, scan in enumerate(list_of_data):
        for j in range(len(EF_LIST)):
            ef = ubtools.EF_LIST[j]
            df.loc[i * len(EF_LIST) + j, :] = [scan.file_name, scan.ei, ef, scan.ei - ef,
                                               scan.tt, scan.mag, scan.converted_dataframes[j],
                                               scan.actual_locus_list[j], scan.planned_locus_list[j]]

    df = df.fillna(nan_fill)
    cut_ei = bin_and_cut(df.ei, en_tolerance)
    cut_en = bin_and_cut(df.en, en_tolerance)
    cut_tt = bin_and_cut(df.tt, tt_tolerance)
    cut_mag = bin_and_cut(df.mag, mag_tolerance)

    if ignore_ef:
        raise NotImplementedError('For the love of god do not try to mix data from different final energies!')
    else:
        df_group = df.groupby([cut_ei, cut_en, cut_tt, cut_mag])
    grouped = df_group['ei', 'ef', 'en', 'tt', 'mag'].mean()
    grouped_data = df_group['points'].apply(series_to_binder).apply(MergedDataPoints)

    grouped_locus_a = df_group['locus_a'].apply(series_to_binder).apply(MergedLocus)
    grouped_locus_p = df_group['locus_p'].apply(series_to_binder).apply(MergedLocus)
    joined = pd.concat([grouped, grouped_data, grouped_locus_a, grouped_locus_p], axis=1)
    index_reset = joined.dropna().reset_index(drop=True)
    return BinnedData(index_reset, ub_matrix=list_of_data[0].ub_matrix)


def read_mf_scan(filename, ub_matrix=None, intensity_matrix=None):
    scan_object = Scan(filename, ub_matrix, intensity_matrix)
    return scan_object


def read_mf_scans(filename_list=None, ub_matrix=None, intensity_matrix=None, processes=1):
    if filename_list is None:
        path = ask_directory('Folder containing data')
        filename_list = list_flexx_files(path)
    if len(filename_list) == 0:
        raise FileNotFoundError('No file to read.')
    arg_list = []
    for name in filename_list:
        arg_list.append((name, ub_matrix, intensity_matrix))
    if processes > 1:
        pool = mp.Pool(processes=processes)
        data_list = pool.starmap(read_mf_scan, arg_list)
    else:
        data_list = list(itertools.starmap(read_mf_scan, arg_list))
    return data_list


def read_and_bin(filename_list=None, ub_matrix=None, intensity_matrix=None, processes=1,
                 en_tolerance=0.05, tt_tolerance=0.5, mag_tolerance=0.05):
    if filename_list is None:
        path = ask_directory('Folder containing data')
        filename_list = list_flexx_files(path)
    items = read_mf_scans(filename_list, ub_matrix, intensity_matrix, processes)
    df = bin_scans(items, en_tolerance=en_tolerance, tt_tolerance=tt_tolerance, mag_tolerance=mag_tolerance)
    return df


class DataBinder(list):
    def __str__(self):
        return '%d items' % len(self)


class MergedLocus(list):
    def __init__(self, items: DataBinder):
        binned_locus = bin_locus(items)
        super(MergedLocus, self).__init__(binned_locus)

    def __str__(self):
        patches = len(self)
        total_vertices = np.sum([len(patch) for patch in self])
        return '%dp %dv' % (patches, total_vertices)


class MergedDataPoints(pd.DataFrame):
    def __init__(self, items: DataBinder):
        binned_points = bin_scan_points(items)
        super(MergedDataPoints, self).__init__(binned_points)

    def __str__(self):
        return '%d pts' % len(self)


class BinnedData(object):
    def __init__(self, source_dataframe: pd.DataFrame, ub_matrix: UBMatrix=None):
        self.data = source_dataframe
        self.ub_matrix = ub_matrix
        self._generate_patch()
        self.last_cut = None
        self.last_plot = None

    def __str__(self):
        return str(pd.concat((self.data[['ei', 'en', 'tt', 'mag']],
                              self.data[['locus_a', 'locus_p', 'points']].astype('str')), axis=1))

    def _generate_patch(self):
        # TODO: defuse this iterator landmine
        list_of_lop = []
        for item in self.data['points']:
            lop = voronoi_plot.generate_vpatch(item['px'], item['py'], self.ub_matrix.figure_aspect)
            list_of_lop.append(lop)
        self.data = self.data.assign(voro=list_of_lop)

    def cut(self, start, end, select=None, precision=2, labels=None, monitor=True):
        """
        1D-cut through specified start and end points.
        :param start: starting point in r.l.u., vector.
        :param end: ending point in r.l.u., vector.
        :param select: a list of indices to cut. Omit to cut all available data.
        :param precision: refer to make_label method.
        :param labels: refer to make_label method.
        :param monitor: if normalize by monitor count.
        :return: ECut object.
        """
        start_p = self.ub_matrix.convert(start, 'rp')[0:2]
        end_p = self.ub_matrix.convert(end, 'rp')[0:2]
        seg = np.vstack([start_p, end_p])
        if select is None:
            select = self.data.index
        cut_results = []
        for index in select:
            df = self.data.loc[index, 'points']
            voro = self.data.loc[index, 'voro']
            included = voronoi_plot.segment_intersect_polygons(seg, voro)
            df_filtered = df.loc[included]
            points = df_filtered[['px', 'py']]
            if monitor:
                intensities = df_filtered['permon']
            else:
                intensities = df_filtered['counts_norm']
            yerr = intensities / np.sqrt(df_filtered['counts'])
            percentiles = voronoi_plot.projection_on_segment(np.array(points), seg, self.ub_matrix.figure_aspect)
            result = pd.DataFrame({'x': percentiles, 'y': intensities, 'yerr': yerr}).sort_values(by='x')
            cut_results.append(result)
        cut_object = ConstECut(cut_results, self, select, start, end)
        self.last_cut = cut_object
        cut_object.plot(precision=precision, labels=labels)
        return cut_object

    def plot(self, select=None):
        plot_object = Plot2D(data_object=self, select=select)
        self.last_plot = plot_object
        return plot_object

    def make_label(self, index, multiline=False, precision=2, columns=None) -> str:
        """
        Makes legend entries for plots.
        :param multiline: If a newline is inserted between each property.
        :param index: Index of record to operate on.
        :param precision: precision of values.
        :param columns: which properties to present in legend. None for all.
        :return: String representing an legend entry.
        """
        if columns is None:
            columns = ['en', 'ef', 'tt', 'mag']
        else:
            for nth, item in enumerate(columns):
                if item not in self.data.columns:
                    columns.pop(nth)

        elements = ['%s=%.*f' % (elem, precision, self.data.loc[index, elem]) for elem in columns]
        if multiline:
            join_char = '\n'
        else:
            join_char = ', '
        return join_char.join(elements)

    def save(self):
        pass


class ConstECut(object):
    def __init__(self, cuts, data_object: BinnedData, indices, start, end):
        self.cuts = cuts
        self.data_object = data_object
        self.indices = indices
        self.figure, self.ax = None, None
        self.artists = None
        self.legend = None
        self.start_r = start
        self.end_r = end

    def to_csv(self):
        pass

    def to_eps(self):
        pass

    def plot(self, precision=2, labels=None):
        self.figure, self.ax = plt.subplots()
        self.artists = []
        ax = self.ax
        for i, cut in enumerate(self.cuts):
            label = self.data_object.make_label(self.indices[i], precision=precision, columns=labels)
            artist = ax.errorbar(cut.x, cut.y, yerr=cut.yerr, fmt='o', label=label)
            self.artists.append(artist)
        self.legend = ax.legend()
        ax.set_ylabel('Intensity (a.u.)')
        start_xlabel = '[' + ','.join(['%.2f' % x for x in self.start_r]) + ']'
        end_xlabel = '[' + ','.join(['%.2f' % x for x in self.end_r]) + ']'
        ax.set_xlabel('Cut from %s to %s' % (start_xlabel, end_xlabel))
        self.figure.tight_layout()

    def __len__(self):
        return len(self.indices)


class Plot2D(object):
    def __init__(self, data_object: BinnedData, select=None, cols=None, aspect=None):
        if select is None:
            select = data_object.data.index
        self.data_object = data_object
        rows, cols = _calc_figure_dimension(len(select), cols)
        self.f, axes = _init_plot_figure(rows, cols)
        self.axes = axes.reshape(-1)
        self.patches = None
        self.indices = select
        self.aspect = aspect
        self.__plot__()

    def __plot__(self):
        self.patches = []
        if self.aspect is None:
            aspect = self.data_object.ub_matrix.figure_aspect
        else:
            aspect = self.aspect
        for nth, index in enumerate(self.indices):
            ax = self.axes[nth]
            ax.grid(ls='--')
            ax.set_axisbelow(True)
            record = self.data_object.data.loc[index, :]
            for locus in record.locus_p:
                locus = np.array(locus)
                ax.plot(locus[:, 0], locus[:, 1], lw=0.2)
            legend_str = self.data_object.make_label(index, multiline=True)
            self.write_label(ax, legend_str)
            values = record.points.permon / record.points.permon.max()
            v_fill = voronoi_plot.draw_patches(record.voro, values)
            coverage_patch = _draw_coverage_patch(ax, record.locus_a)
            ax.add_collection(v_fill)
            v_fill.set_clip_path(coverage_patch)
            ax.set_aspect(aspect)
            xlabel, ylabel = ubtools.guess_axes_labels(self.data_object.ub_matrix.plot_x,
                                                       self.data_object.ub_matrix.plot_y)
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)

            ax.set_xlim([record.points.px.min(), record.points.px.max()])
            ax.set_ylim([record.points.py.min(), record.points.py.max()])
            self.patches.append(v_fill)
        self.f.tight_layout()

    def to_eps(self):
        pass

    def cut(self, start, end, select=None, precision=2, labels=None, monitor=True):
        if select is None:
            select = self.indices
        else:
            select = [self.indices[x] for x in select]
        cut_obj = self.data_object.cut(start, end, select, precision, labels, monitor)
        return cut_obj

    @staticmethod
    def write_label(ax, text):
        ax.text(1.00, 1.00,
                text,
                transform=ax.transAxes, zorder=200, color='black',
                bbox={'facecolor': 'white', 'alpha': 0.8, 'pad': 5}, horizontalalignment='right',
                verticalalignment='top')

    def set_norm(self, norm):
        for patch in self.patches:
            patch.set_norm(norm)


def _calc_figure_dimension(no_plots, cols=None):
    if cols is None:
        if no_plots == 1:
            return 1, 1
        elif no_plots == 2:
            return 1, 2
        elif no_plots == 3:
            return 1, 3
        else:
            sqroot = np.sqrt(no_plots)
            if sqroot == int(sqroot):
                return int(sqroot), int(sqroot)
            else:
                cols = int(sqroot) + 1
                if cols * (cols - 1) < no_plots:
                    rows = cols
                else:
                    rows = cols - 1
                return int(rows), int(cols)
    else:
        if no_plots % cols == 0:
            rows = no_plots / cols
        else:
            rows = no_plots / cols + 1
        return int(rows), int(cols)


def _init_plot_figure(rows, cols):
    return np.array(plt.subplots(rows, cols, sharex='all', sharey='all'))


def _draw_coverage_patch(ax_handle, locus):
    mpath_path = mpl_path.Path
    combined_verts = np.zeros([0, 2])
    combined_codes = []
    for each in locus:
        codes = [mpath_path.LINETO] * len(each)
        codes[0], codes[-1] = mpath_path.MOVETO, mpath_path.CLOSEPOLY
        combined_codes += codes
        combined_verts = np.vstack([combined_verts, each])
    path = mpath_path(combined_verts, combined_codes)
    patch = mpl_patches.PathPatch(path, facecolor='k', alpha=0, zorder=10)
    ax_handle.add_patch(patch)
    return patch


def ask_directory(title='Choose a folder'):
    root = tkinter.Tk()
    root.withdraw()
    path = filedialog.askdirectory(initialdir='.', title=title)
    return path


def list_flexx_files(path):
    file_names = [os.path.join(path, s) for s in os.listdir(path) if (s.isdigit() and len(s) == 6)]
    return file_names


def _unpack_user_hkl(user_input: str):
    unpacked = [float(s) for s in user_input.split(',')]
    if len(unpacked) != 3:
        raise ValueError('Not a valid h, k, l input.')

    return unpacked
