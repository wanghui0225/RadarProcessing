import numpy as np
import scipy.optimize as opt
import scipy.integrate as integrate
import matplotlib.pyplot as plt
import matplotlib.pylab as plb
import matplotlib.patches as mpatches

import netCDF4 as nc
import os

import pulp as pl
from sklearn.mixture import GaussianMixture as GMM

from radarVis import *


fuzzy_dict = {"clutter":
                {"Z": {"m": -99900.0, "a": -99900.0, "b": -99900.0, "w":-99900.0},
                 "zDr": {"m": -99900.0, "a": -99900.0, "b": -99900.0, "w": -99900.0},
                 "zDr_std": {"m": 2.0, "al": 0.8, "bl": 1.8, "ar": 1000, "br": 2, "w": 0.5},
                 "PhiDP": {"m": -99900.0, "a": -99900.0, "b": -99900.0, "w": -99900.0},
                 "PhiDP_std": {"m": 40, "al": 16.0, "bl": 1.8, "ar": 1000, "br": 2, "w": 1.0},
                 "RhoHV": {"m": 0.5, "al": 0.4, "bl": 1.0, "ar": 0.3, "br": 2.0, "w": 0.2},
                 "KDP": {"m": -99900.0, "a": -99900.0, "b": -99900.0, "w": -99900.0},
                 "T": {"m": -99900.0, "a": -99900.0, "b": -99900.0, "w": -99900.0}
                }
            }


# ************************* General Math functions *************************
def rolling_window(dta, window_length):
    shape = dta.shape[:-1] + (dta.shape[-1] - window_length + 1, window_length)
    strides = dta.strides + (dta.strides[-1], )
    dta_window = np.lib.stride_tricks.as_strided(dta, shape=shape, strides=strides)
    return dta_window


def memFunc(x, m, al, bl, ar=None, br=None):
    if ar is None:
        ar = al
    if br is None:
        br = bl
    mf = np.where(x<=m, 1.0/(1.0+np.power(np.abs((x-m)/al), 2.0*bl)), 1.0/(1.0+np.power(np.abs((x-m)/ar), 2.0*br)))

    return mf


def get_dispersion(dta, axis=None):
    dta_c = np.cos(dta)
    dta_s = np.sin(dta)
    return np.var(dta_c, axis=axis) + np.var(dta_s, axis=axis)


def get_LinearCoef(y, x, axis=None):
    y_mean = np.mean(y, axis=axis)
    x_mean = np.mean(x, axis=axis)

    if axis is not None:
        y_mean = y_mean.reshape((-1, 1))
        x_mean = x_mean.reshape((-1, 1))

    coef = np.mean((y - y_mean)*(x - x_mean), axis=axis) / np.mean((x - x_mean)*(x - x_mean), axis=axis)

    return coef


def get_invW(dta, axis=None):
    num_sample = dta.shape[1]
    num_padding = int((num_sample-1)/2)
    real_avg = np.mean(np.real(dta), axis=axis)
    img_avg = np.mean(np.imag(dta), axis=axis)
    invW = np.sqrt(1.0 - np.sqrt(real_avg**2+img_avg**2))
    invW = np.concatenate(([invW[0] for i in range(0, num_padding)], invW, [invW[-1] for i in range(0, num_padding)]), axis=0)

    return np.diag(invW)


def complex2deg(complex_array):
    deg_array = np.angle(complex_array, deg=True)
    return np.where(deg_array < 0.0, deg_array + 2*180.0, deg_array)


# ************************* Visualization *************************
def draw_ellipse(pos, covariance, ax=None, **kwargs):
    # Convert covariance to principal axes
    if covariance.shape == (2, 2):
        U, sig_Val, Vt = np.linalg.svd(covariance)
        angle = np.degrees(np.arctan2(U[1, 0], U[0, 0]))
        width, height = 2.0 * np.sqrt(2.0) * np.sqrt(sig_Val)
    else:
        angle = 0
        width, height = 2.0 * np.sqrt(2.0) * np.sqrt(covariance)

    ax.add_patch(mpatches.Ellipse(pos, width, height, angle, **kwargs))


def plot_gmm(gmm_model, X, label=True, ax=None):
    labels = gmm_model.fit(X).predict(X)
    if label:
        ax.scatter(X[:, 0], X[:, 1], c=labels, s=40, cmap='viridis', zorder=2)
    else:
        ax.scatter(X[:, 0], X[:, 1], s=40, zorder=2)

    w_factor = 0.2 / gmm_model.weights_.max()
    for pos, covar, w in zip(gmm_model.means_, gmm_model.covars_, gmm_model.weights_):
        draw_ellipse(pos, covar, alpha=w * w_factor)


def plot_label(GateWidth_r, Phi_dp_array, labels):
    fig, ax = plt.subplots(3, 2, figsize=(10, 12))
    loc_dic = {0: [0, 0], 60: [0, 1], 120: [1, 0], 180: [1, 1], 240: [2, 0], 300: [2, 1]}

    for r in loc_dic.keys():
        ir, jr = loc_dic[r]
        n_color = len(np.bincount(labels)) - 1
        cmap = plt.cm.get_cmap("jet", n_color)

        clutter_loc = np.nonzero(labels == 0)[0]
        ax[ir, jr].scatter(GateWidth_r[clutter_loc], Phi_dp_array[r, clutter_loc], c="darkgrey", label=0, s=0.5)

        for l in range(0, n_color):
            weather_loc = np.nonzero(labels == l+1)[0]
            ax[ir, jr].scatter(GateWidth_r[weather_loc], Phi_dp_array[r, weather_loc], c=[cmap(l)], label=l+1, s=1)

        ax[ir, jr].set_title("No. Radial = %d" % r)
        ax[ir, jr].legend(fontsize="small", ncol=4)

    # plt.show()
    plt.savefig("DROPs_masking_pop1_combine.png", dpi=400)


# ************************* Quality Control Algorithms *************************
# ************* [1] Phase Reconstruction *************
# ---Phi_dp unfolding (necessary for non-angular method)
def PhaseUnfolding(Phi_dp_array, rho_hv_array, GateWidth_array, max_phaseDiff=-80, dphase=180):
    std_start = 5.0
    rho_hv_start = 0.9
    std_ref = 15.0
    slope_min_ref = -5.0
    slope_max_ref = 20.0

    ref = 0.0
    pt_start = 0
    num_radial, num_gate = Phi_dp_array.shape
    for r in range(0, num_radial):
        w_r = GateWidth_array[r]
        GateWidth_r = np.ones(num_gate) * w_r
        GateWidth_r = np.cumsum(GateWidth_r)
        # ------determine the start point of meaningful Phi_dp
        for i in range(5, num_gate - 9):
            if (np.std(Phi_dp_array[r, i:i+10]) < std_start) and (np.all(rho_hv_array[r, i-5:i] > rho_hv_start)):
                ref = np.mean(Phi_dp_array[r, i-5:i])
                pt_start = i
                break

        for i in range(pt_start, num_gate - 9):
            sigma = np.std(Phi_dp_array[r, i:i+10])
            # ---------calculate the slope of Linear Regression over the last 5 gates
            # ---------slope = Cov(Phi_dp, r) / Var(r)
            cov_mat = np.cov(GateWidth_r[i-5:i], Phi_dp_array[r, i-5:i])
            slope = cov_mat[0, 1] / cov_mat[0, 0]
            if (sigma < std_ref) and (slope > slope_min_ref) and (slope < slope_max_ref):
                ref += slope * w_r
            if Phi_dp_array[r, i] - ref < max_phaseDiff:
                Phi_dp_array[r, i] += dphase

    return Phi_dp_array


# ---reconstruct Phi_dp using Linear Programming
def LP_solver(Phi_dp_array):
    num_gate = Phi_dp_array.shape[0]

    # ------define the Problem object
    lp_prob = pl.LpProblem("Reconstruction", pl.LpMinimize)
    # ------define the decision variable
    z = ["z" + str(i) for i in range(0, num_gate)]
    x = ["x" + str(i) for i in range(0, num_gate)]
    var = z + x
    lp_var = pl.LpVariable.dicts("var", var, cat='Continuous')
    # ------define the objective function
    lp_prob += pl.lpSum([lp_var[var[i]] for i in range(0, num_gate)])
    # ------add the fidelity constraints
    for i in range(0, num_gate):
        lp_prob += lp_var[var[i]] - lp_var[var[i + num_gate]] >= -Phi_dp_array[i]
    for i in range(0, num_gate):
        lp_prob += lp_var[var[i]] + lp_var[var[i + num_gate]] >= Phi_dp_array[i]
    # ------add the montonicity constraints via five-point Savitzky–Golay derivative filter
    for i in range(num_gate + 2, 2 * num_gate - 2):
        lp_prob += -0.2 * lp_var[var[i-2]] + (-0.1) * lp_var[var[i-1]] + 0.1 * lp_var[var[i+1]] + 0.2 * lp_var[var[i+2]] >= 0.0

    # ------solve the LP problem
    # ---------use GNU Linear Programming Kit solver
    # lp_prob.solve(pl.GLPK())
    # ---------use default CBC solver
    lp_prob.solve()
    # ------check the status of solution
    if lp_prob.status == 1:
        phi_rec = np.array([lp_var[var[i]].varValue for i in range(num_gate, 2*num_gate)])
        # ------smoothing filter to prevent subﬁlter-length oscillations
        Phi_dp_array[2:num_gate-2] = 0.1 * phi_rec[0:num_gate-4] + 0.25 * phi_rec[1:num_gate-3] \
                                     + 0.3 * phi_rec[2:num_gate-2] + 0.25 * phi_rec[3:num_gate-1] + 0.1 * phi_rec[4:num_gate]
        Phi_dp_array[num_gate-2] = Phi_dp_array[num_gate-3]
        Phi_dp_array[num_gate-1] = Phi_dp_array[num_gate-3]
    else:
        print("Linear Programming failed !!!")

    return Phi_dp_array


def PhaseRec_LP(Phi_dp_array, KDP_array, rho_hv_array, GateWidth_array, num_good=15, num_bad=10, d_max=0.98, rho_max=0.9, population_min=5):
    num_radial, num_gate = Phi_dp_array.shape

    for r in range(0, num_radial):
        # ------rain cell segmentation
        w_r = GateWidth_array[r]
        GateWidth_r = np.full(num_gate, w_r)
        GateWidth_r = np.cumsum(GateWidth_r)
        labels = dataMasking_DROPs(Phi_dp_array[r], GateWidth_r, rho_hv_array[r], num_good, num_bad, d_max, rho_max, population_min)
        labels = labels.astype(np.int64)
        # ------use Linear Programming in every rain cell
        n_cell = len(np.bincount(labels)) - 1
        for cid in range(1, n_cell + 1):
            cell_loc = np.nonzero(labels == cid)[0]
            Phi_dp_array[r, cell_loc] = LP_solver(Phi_dp_array[r, cell_loc])
            array_split = rolling_window(Phi_dp_array[r, cell_loc], 5)
            KDP_array_r = (-0.2*array_split[:, 0] - 0.1*array_split[:, 1] + 0.1*array_split[:, 3] + 0.2*array_split[:, 4]) / w_r
            KDP_array[r, cell_loc] = np.concatenate(([KDP_array_r[0]], [KDP_array_r[0]], KDP_array_r, [KDP_array_r[-1]], [KDP_array_r[-1]]), axis=0)

    return Phi_dp_array, KDP_array


# ---reconstruct Phi_dp using Fuzzy Logic
def PhaseRec_fuzzy(reflectivity_array, zDr_array, Phi_dp_array, RhoHV_array, KDP_array, T_array, GateWidth_array):
    num_radial, num_gate = Phi_dp_array.shape
    num_sample = 11
    num_padding = int((num_sample-1)/2)

    fig, ax = plt.subplots(3, 2, figsize=(10, 12))
    loc_dic = {0: [0, 0], 60: [0, 1], 120: [1, 0], 180: [1, 1], 240: [2, 0], 300: [2, 1]}

    for r in range(0, num_radial):
        zDr_std = np.std(rolling_window(zDr_array[r], num_sample), 1)
        zDr_std = np.concatenate(([zDr_std[0] for i in range(0, num_padding)], zDr_std, [zDr_std[-1] for i in range(0, num_padding)]), axis=0)
        mf_zDr = memFunc(zDr_std, fuzzy_dict["clutter"]["zDr_std"]["m"], fuzzy_dict["clutter"]["zDr_std"]["al"], fuzzy_dict["clutter"]["zDr_std"]["bl"],
                         fuzzy_dict["clutter"]["zDr_std"]["ar"], fuzzy_dict["clutter"]["zDr_std"]["br"])
        phi_dp_std = np.std(rolling_window(Phi_dp_array[r], num_sample), 1)
        phi_dp_std = np.concatenate(([phi_dp_std[0] for i in range(0, num_padding)], phi_dp_std, [phi_dp_std[-1] for i in range(0, num_padding)]), axis=0)
        mf_phi_dp = memFunc(phi_dp_std, fuzzy_dict["clutter"]["PhiDP_std"]["m"], fuzzy_dict["clutter"]["PhiDP_std"]["al"], fuzzy_dict["clutter"]["PhiDP_std"]["bl"],
                         fuzzy_dict["clutter"]["PhiDP_std"]["ar"], fuzzy_dict["clutter"]["PhiDP_std"]["br"])
        mf_RhoHV = memFunc(RhoHV_array[r], fuzzy_dict["clutter"]["RhoHV"]["m"], fuzzy_dict["clutter"]["RhoHV"]["al"], fuzzy_dict["clutter"]["RhoHV"]["bl"],
                            fuzzy_dict["clutter"]["RhoHV"]["ar"], fuzzy_dict["clutter"]["RhoHV"]["br"])
        s = (fuzzy_dict["clutter"]["zDr_std"]["w"] * mf_zDr + fuzzy_dict["clutter"]["PhiDP_std"]["w"] * mf_phi_dp
             + fuzzy_dict["clutter"]["RhoHV"]["w"] * mf_RhoHV) / (fuzzy_dict["clutter"]["zDr_std"]["w"] + fuzzy_dict["clutter"]["PhiDP_std"]["w"] + fuzzy_dict["clutter"]["RhoHV"]["w"])

        if r in loc_dic.keys():
            ir, jr = loc_dic[r]
            clutter_loc = np.nonzero(s >= 0.5)
            weather_loc = np.nonzero(s < 0.5)

            w_r = GateWidth_array[r]
            GateWidth_r = np.full(num_gate, w_r)
            GateWidth_r = np.cumsum(GateWidth_r)

            ax[ir, jr].scatter(GateWidth_r[clutter_loc], Phi_dp_array[r, clutter_loc], c="darkgrey", label="clutter", s=5)
            ax[ir, jr].scatter(GateWidth_r[weather_loc], Phi_dp_array[r, weather_loc], c="royalblue", label="weather", s=5)

            ax[ir, jr].set_title("No. Radial = %d" % r)
            ax[ir, jr].legend(fontsize="small", ncol=4)

    # plt.show()
    plt.savefig("fuzzy_masking.png", dpi=400)


# ---reconstruct Phi_dp using Gaussian Mixture Model
def PhaseRec_GMM(Phi_dp_array, reflectivity_array, GateWidth_array):
    num_radial, num_gate = Phi_dp_array.shape

    zH_limit = 40.0
    ratio_low = 15.0
    sigma_low = 4.0
    ratio_high = 50.0
    sigma_high = 6.0

    # ------define the Gaussian Mixture Model
    n_model = 11
    gmm_model_list = [GMM(n_components=int(i), covariance_type="diag", init_params="kmeans", n_init=3) for i in np.linspace(5, 25, 11)]

    fig, ax = plt.subplots(3, 2, figsize=(10, 12))
    loc_dic = {0: [0, 0], 60: [0, 1], 120: [1, 0], 180: [1, 1], 240: [2, 0], 300: [2, 1]}

    for r in range(0, num_radial):
        w_r = GateWidth_array[r]
        GateWidth_r = np.full(num_gate, w_r)
        GateWidth_r = np.cumsum(GateWidth_r)
        dp_r = np.concatenate((GateWidth_r.reshape(-1, 1), Phi_dp_array[r].reshape(-1, 1)), axis=1)
        # ------Data masking
        # ---------select the best GMM via the Bayesian Information Criteria
        bic_min = np.infty
        m_id = 0
        for i in range(0, n_model):
            gmm_model_list[i].fit(dp_r)
            bic_r = gmm_model_list[i].bic(dp_r)
            if bic_r < bic_min:
                m_id = i
                bic_min = bic_r
        # ---------mask out the clusters with no more than 5 points
        labels = gmm_model_list[m_id].predict(dp_r)
        label_count = np.bincount(labels)
        label_mask = np.nonzero(label_count <= 5)[0]
        # ---------mask out the clusters with too high sigma(phi_dp)
        for l in range(0, len(label_count)):
            if (len(label_mask) == 0) or (len(label_mask) > 0 and l not in label_mask):
                loc = np.nonzero(labels == l)
                zH_mean = np.mean(reflectivity_array[r, loc])
                sigma_r = np.std(GateWidth_r[loc])
                sigma_phi_dp = np.std(Phi_dp_array[r, loc])
                if zH_mean < zH_limit:
                    if sigma_phi_dp / sigma_r >= ratio_low or sigma_phi_dp >= sigma_low:
                        label_mask = np.append(label_mask, l)
                else:
                    if sigma_phi_dp / sigma_r >= ratio_high or sigma_phi_dp >= sigma_high:
                        label_mask = np.append(label_mask, l)

        if r in loc_dic.keys():
            ir, jr = loc_dic[r]
            n_color = len(label_count) - len(label_mask)
            cmap = plb.get_cmap("jet", n_color)
            counter = 0
            for l in range(0, len(label_count)):
                loc = np.nonzero(labels == l)
                if (len(label_mask) == 0) or (len(label_mask) > 0 and l not in label_mask):
                    ax[ir, jr].scatter(GateWidth_r[loc], Phi_dp_array[r, loc], c=[cmap(counter)], label=l, s=5)
                    counter += 1
                else:
                    ax[ir, jr].scatter(GateWidth_r[loc], Phi_dp_array[r, loc], c="darkgrey", label=l, s=5)
            ax[ir, jr].set_title("No. Radial = %d" % r)
            ax[ir, jr].legend(fontsize="small", ncol=4)
            print(r, label_mask)
    #plt.show()
    plt.savefig("GMM_masking.png", dpi=400)

    return Phi_dp_array


# ---reconstruct Phi_dp followed the method proposed by DROPs
def dataMasking_DROPs(Phi_dp_array, GateWidth_array, rho_hv_array, num_good, num_bad, d_max, rho_max, population_min):
    # ------calculate the dispersion of PhiDP
    d_PhiDP = get_dispersion(rolling_window(Phi_dp_array, num_good), axis=1)
    d_PhiDP = np.concatenate((d_PhiDP, [d_PhiDP[-1] for i in range(0, num_good - 1)]), axis=0)

    id_start = np.where(d_PhiDP >= d_max)[0]
    id_end = np.where((d_PhiDP < d_max) & (rho_hv_array < rho_max))[0]

    # ------rain cell segmentation
    counter = 0
    i_end_prev = 0
    cell_id = 1
    labels = np.zeros_like(GateWidth_array)
    num_start = len(id_start)
    num_end = len(id_end)
    while counter < num_start:
        i_start = id_start[counter]
        # --------search the index of the end position of this rain cell
        counter = np.searchsorted(id_end, i_start, side="left")
        # -----------stop if end index exceeds
        if counter >= num_end:
            break
        else:
            i_end = id_end[counter]
            # ----------combine two neighbouring rain cells
            if i_start - i_end_prev == 1:
                # ----------assign the previous label
                labels[i_start:i_end + 1] = cell_id - 1
            else:
                # ----------only keep rain cells which contain enough points
                if i_end - i_start + 1 >= population_min:
                    labels[i_start:i_end + 1] = cell_id
                    cell_id += 1
            # ----------update the previous end position
            i_end_prev = i_end
            # ----------search the index of the start position of the next rain cell
            counter = np.searchsorted(id_start, i_end, side="left")

    return labels


def PhaseRec_DROPs(Phi_dp_array, GateWidth_array, rho_hv_array, KDP_array, num_good=15, num_bad=10, d_max=0.98, rho_max=0.9, population_min=5, record_list=None):
    num_radial, num_gate = Phi_dp_array.shape

    for r in range(0, num_radial):
        w_r = GateWidth_array[r]
        GateWidth_r = np.full(num_gate, w_r)
        GateWidth_r = np.cumsum(GateWidth_r)
        labels = dataMasking_DROPs(Phi_dp_array[r], GateWidth_r, rho_hv_array[r], num_good, num_bad, d_max, rho_max, population_min)
        labels = labels.astype(np.int64)

        Kdp_dict = None
        if record_list is not None:
            Kdp_dict = dict.fromkeys(record_list)

        # ------Do cubic spline fitting in every rain cell
        n_cell = len(np.bincount(labels)) - 1
        for cid in range(1, n_cell+1):
            cell_loc = np.nonzero(labels == cid)[0]
            h_vec = np.full(cell_loc.shape[0], w_r)  # len(h_vec) = M
            Phi_dp_i = np.cos(Phi_dp_array[r, cell_loc]/180.0*np.pi) + 1.0j * np.sin(Phi_dp_array[r, cell_loc]/180.0*np.pi)
            # ------First, we need to do non-adaptive fitting to obtain the overall trend of the Kdp proﬁle
            val_lambda = 0.1 * w_r
            # ------------prepare coefficient matrix M, Q
            p_vec = np.full(cell_loc.shape[0] - 2, 4 * w_r)
            M_mat = np.diag(p_vec) + np.diag(h_vec[1:-2], k=1) + np.diag(h_vec[1:-2], k=-1)
            l_vec = 3.0 / h_vec[:-1]     # len(l_vec) = M-1
            u_vec = -l_vec[1:] - l_vec[:-1]    # len(u_vec) = M-2
            Q_mat = np.diag(l_vec[:-1]) + np.diag(u_vec[:-1], k=1) + np.diag(l_vec[1:-2], k=2)  # size(Q_mat) = (M-2)*M
            Q_last_col = np.zeros((Q_mat.shape[0], 1))
            Q_mat = np.concatenate((Q_mat, Q_last_col, Q_last_col), axis=1)
            Q_mat[-1, -1] = l_vec[-1]
            Q_mat[-2, -2] = l_vec[-2]
            Q_mat[-1, -2] = u_vec[-1]
            b_ini = np.linalg.inv(M_mat + 2.0/3.0/val_lambda*np.dot(Q_mat, np.transpose(Q_mat)))
            b_ini = np.dot(np.dot(b_ini, Q_mat), Phi_dp_i)
            d_ini = Phi_dp_i - 2.0/3.0/val_lambda * np.dot(np.transpose(Q_mat), b_ini)
            b_ini = np.concatenate(([0], b_ini, [0]), axis=0)
            c_ini = (d_ini[1:] - d_ini[:-1]) / h_vec[:-1] - (b_ini[1:] + 2.0*b_ini[:-1]) * h_vec[:-1] / 3.0
            KDP_ini = 0.5 * np.imag(c_ini / d_ini[:-1])
            KDP_ini = np.concatenate((KDP_ini, [KDP_ini[-1]]), axis=0)
            # ------Then, we do adaptive cubic spline fitting in every rain cell
            val_lambda = 1.1 * w_r
            # ---------calculate the weighting matrix W for the precision of fitting
            W_inv_mat = get_invW(rolling_window(Phi_dp_i, 5), 1)
            # ---------calculate the weighting matrix Mq for the smoothness of fitting
            # ------------replace noData value of KDP observation using 999999.0
            w_q = 1.0 / (2.0 * KDP_ini)
            w_q = np.where(w_q > 5.0, 5.0, w_q)
            qh_vec = w_q[:-1] * h_vec[:-1]
            p_vec = 2 * (qh_vec[:-1] + qh_vec[1:])
            Mq_mat = np.diag(p_vec) + np.diag(qh_vec[1:-1], k=1) + np.diag(qh_vec[1:-1], k=-1)
            # ---------calculate the solution of cubic spline fitting
            # ------------coefficient matrix remains unchanged
            # ------------solve b = [b2, b3, ..., b_{M-1}]^T and d = [d1, d2, ..., d_M]^T
            cum_mat = np.dot(W_inv_mat, np.transpose(Q_mat))
            cum_mat = np.dot(cum_mat, np.linalg.inv(M_mat))
            cum_mat = np.dot(cum_mat, Mq_mat)
            b_vec = np.linalg.inv(M_mat + 2.0/3.0/val_lambda * np.dot(Q_mat, cum_mat))
            b_vec = np.dot(np.dot(b_vec, Q_mat), Phi_dp_i)   # len(b_vec) = M-2
            # ---------------Note that d corresponds to the smoothed angular Phi_dp
            d_vec = Phi_dp_i - 2.0/3.0/val_lambda * np.dot(cum_mat, b_vec)   # len(d_vec) = M
            Phi_dp_array[r, cell_loc] = complex2deg(d_vec)
            # ---------------b1, b_M are set to 0 because of the imposed natural condition
            b_vec = np.concatenate(([0], b_vec, [0]), axis=0)
            # ------------solve a = [a1, a2, ..., a_{M-1}]^T and c = [c1, c2, ..., c_{M-1}]^T
            c_vec = (d_vec[1:] - d_vec[:-1]) / h_vec[:-1] - (b_vec[1:] + 2.0*b_vec[:-1]) * h_vec[:-1] / 3.0
            KDP_array[r, cell_loc[:-1]] = 0.5 * np.imag(c_vec/d_vec[:-1])

        if r in record_list:
            Kdp_dict[r] = KDP_array[r]

    return Phi_dp_array, KDP_array


# ************* [2] Attenuation Correction *************
# ---attenuation correction by Z-PHI method
def get_para_a(reflect_integral, PIA, b=0.78):
    # ------Ref to
    # Testud, Jacques, et al. “The Rain Profiling Algorithm Applied to Polarimetric Weather Radar.”
    # Journal of Atmospheric and Oceanic Technology, vol. 17, no. 3, 2000, pp. 332–356.
    return 1.0 - np.exp(-0.23*b*PIA) / 0.46 / reflect_integral


def phase_loss(c_coef, reflectivity_array, Phi_dp_array, GateWidth_r, r_start, num_gate, b):
    PIA = c_coef * (Phi_dp_array - Phi_dp_array[r_start])
    reflect_integral = np.array([integrate.simps(y=np.power(reflectivity_array[r_start:i], b), x=GateWidth_r[r_start:i]) for i in range(r_start, num_gate)])
    a = get_para_a(reflect_integral, PIA, b=b)
    attenuation = a * np.power(reflectivity_array, b) / (1.0 - 0.46 * a * b * reflect_integral)
    Phi_dp_rec = np.array([integrate.simps(y=attenuation[0:i-r_start]/a, x=GateWidth_r[r_start:i]) for i in range(r_start, num_gate)])
    return np.mean(np.abs(Phi_dp_rec - Phi_dp_array[r_start:]))


def correct_ZPHI(reflectivity_array, zDr_array, Phi_dp_array, GateWidth_array, r_start, c_min=0.03, c_max=0.18):
    num_radial, num_gate = zDr_array.shape

    b_coef = 0.78
    # c_coef = c_min

    for r in range(0, num_radial):
        w_r = GateWidth_array[r]
        GateWidth_r = np.full(num_gate - r_start, w_r)
        GateWidth_r = np.cumsum(GateWidth_r)
        # ------find the optimal c by minimizing cost function
        opt_res = opt.differential_evolution(func=phase_loss, args=(reflectivity_array, Phi_dp_array, GateWidth_r, r_start, num_gate, b_coef),
                                             tol=1e-4, bounds=(c_min, c_max))
        c_coef = opt_res["x"]
        # ------calculate Path-Integrated Attenuation (PIA)
        PIA = c_coef * (Phi_dp_array - Phi_dp_array[r_start])
        # ------calculate coefficient a
        reflect_integral = np.array([integrate.simps(y=np.power(reflectivity_array[r_start:i], b_coef), x=GateWidth_r[r_start:i]) for i in range(r_start, num_gate)])
        a_coef = get_para_a(reflect_integral, PIA, b=b_coef)
        reflectivity_array[r_start:] = reflectivity_array[r_start:] / np.power(1.0 - 0.46 * a_coef * b_coef * reflect_integral, 1.0/b_coef)



raw_dir = "Input"
raw_fname = "BJXFS_2.5_20190909_180000.netcdf"
nc_ds = nc.Dataset(os.path.join(raw_dir, raw_fname), "r")

# convert mm to km
GateWidth = np.array(nc_ds.variables["GateWidth"]) / 1000.0 / 1000.0
zDr = np.array(nc_ds.variables["DifferentialReflectivity"])
Phi_dp = np.array(nc_ds.variables["DifferentialPhase"])
rho_hv = np.array(nc_ds.variables["CrossPolCorrelation"])
KDP = np.array(nc_ds.variables["KDP"])
reflectivity = np.array(nc_ds.variables["Reflectivity"])

temperature = np.zeros_like(reflectivity)

# Phi_dp_unfold = PhaseUnfolding(Phi_dp, rho_hv, GateWidth, max_phaseDiff=-180, dphase=360)
# Phi_dp_rec, Kdp_rec = PhaseRec_LP(Phi_dp, KDP, rho_hv, GateWidth)
# Phi_dp_rec = PhaseRec_GMM(Phi_dp, reflectivity, GateWidth)
# PhaseRec_fuzzy(reflectivity, zDr, Phi_dp, rho_hv, KDP, temperature, GateWidth)
# Phi_dp_rec, Kdp_rec = PhaseRec_DROPs(Phi_dp, GateWidth, rho_hv, KDP, d_max=0.95, record_list=[0, 60, 120, 180, 240, 300])

'''
num_radial, num_gate = Phi_dp.shape
GateWidth_cum = np.full(num_gate, GateWidth[0])
GateWidth_cum = np.cumsum(GateWidth_cum)
ppi_vis(Kdp_rec, "DROPs_KDP.png", range=GateWidth_cum, title="DROPs-processed $K_{dp}$ at 2019/09/09 18:00:00", colorbar_label="$K_{dp}$ [Degrees/$\mathrm{km}$]", noData=-5.0)
'''
