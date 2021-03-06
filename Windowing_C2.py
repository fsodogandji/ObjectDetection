#!/usr/bin/python

from glimpse.glab import LoadExperiment
from glimpse.glab import *
from glimpse.models.viz2 import Model as Viz2Model
from glimpse.models.viz2.layer_mapping import RegionMapper
from glimpse.util import svm
import math
import Image
import ImageDraw
import logging
import numpy as np
import os
import sys
import time
import copy
import pickle

def MakeBoundingBoxes(height, width, step_size, box_size):
  """Create bounding boxes in the X-Y plane.
  height, width -- (int) size of the plane
  step_size -- (int) distance between top-left corner of adjacent boxes
  box_size -- (int) width and height of each box
  RETURN (2D list) chosen bounding boxes in the format (y0, y1, x0, x1), where
  (y0, x0) gives the upper-left corner of the box (i.e., y0 and x0 are
  inclusive), and (y1, x1) gives the unit just beyond the lower-right corner
  (i.e., y1 and x1 are exclusive).
  """
  # Holds the sliding windows for the given layer
  windows = list()
  # Create bounding boxes for the given scale layer over all positions in the c1
  # map
  for j in range(0, height - box_size, step_size):
    y0, y1 = j, j + box_size
    for i in range(0, width - box_size, step_size):
      x0, x1 = i, i + box_size
      bbox = np.array([y0, y1, x0, x1], np.int)
      windows.append(bbox)
  #print "List of Windows", windows
  return windows

def ScaleImage(image, scale):
  """Scale an image (either up-sampling or down-sampling) by a given ratio."""
  width, height = image.size
  width = int(width * scale)
  height = int(height * scale)
  return image.resize((width, height), Image.ANTIALIAS)

class Windower(object):

  def __init__(self, glimpse_model, svm_model, step_size, bbox_size,
      debug = False):
    assert isinstance(glimpse_model, Viz2Model), "Wrong model type"
    self.glimpse_model = glimpse_model


    self.svm_model = svm_model
    self.step_size = step_size
    self.bbox_size = bbox_size
    self.debug = debug
    self.mapper = RegionMapper(glimpse_model.params)

  def ChooseImageScales(self, image_size):
    """Choose the scaling (down-sampling) ratios for a given image. This is a member function so
    that the scaling algorithm can be easily overridden."""
    image_width = image_size[0]
    # Choose image scales by requesting a fixed image size, as a offset from the
    # current image width.
    num_scales = 30
    new_widths = image_width - np.arange(0, 1000, int(1000 / float(num_scales)))
    return new_widths / float(image_width)

  def MapC1RegionToImageBox(self, bbox, scale):
    """ Map C1 layer coordinates to the corresponding coordinates in image
    space, and adjusts for image scaling.
    bbox -- (1D array-like) C1 region in the format of (y0, y1, x0, x1)
    scale -- (float) scaling ratio between scaled image size and original input
             image size
    RETURN (1D np.array) bounding box in original input image coordinates.
    """
    c1_y0, c1_y1, c1_x0, c1_x1 = bbox
    c1_yrange = slice(c1_y0, c1_y1)
    c1_xrange = slice(c1_x0, c1_x1)
    img_yrange = self.mapper.MapS2ToImage(c1_yrange)
    img_xrange = self.mapper.MapS2ToImage(c1_xrange)
    img_y0, img_y1 = img_yrange.start, img_yrange.stop
    img_x0, img_x1 = img_xrange.start, img_xrange.stop
    return (np.array([img_x0, img_y0, img_x1, img_y1]) / scale).astype(np.int)

  def ClassifyC2Window(self, crop):
    """Compute SVM output for a single C1 crop.
    crop -- (3D np.array) region of C1 activity
    RETURN predicted label and decision value
    """

    crop_roll = np.rollaxis(crop,1)
    #~ print "Crop Roll",crop_roll.shape
    crop_prime = crop_roll.reshape(len(crop_roll),-1)

    #~ print "Shape of crop roll", crop_prime.shape

    c2_crop = crop_prime.max(1)
    pos_instances = [c2_crop]
    neg_instances = []
    all_instances = pos_instances, neg_instances
    # Prepare the data
    test_features, test_labels = svm.PrepareFeatures(all_instances)
    # Evaluate the classifier
    predicted_labels = self.svm_model.predict(test_features)
    decision_values = self.svm_model.decision_function(test_features)
    return predicted_labels, decision_values

  def ProcessScale(self, scaled_image):
    logging.info("ProcessScale() -- scaled image size: %s" % \
        (scaled_image.size,))
    # Compute C1 layer activity.
    image_layer = self.glimpse_model.MakeStateFromImage(scaled_image)
    output_layer = self.glimpse_model.LayerClass.S2
    print "NUMBER OF PROTOTYPES", len(self.glimpse_model.s2_kernels[0])

    output_state = self.glimpse_model.BuildLayer(output_layer, image_layer,
        save_all = False)

    s2_layer = np.array(output_state[output_layer])
    logging.info("s2 layer shape for scale: %s" % (s2_layer.shape,))
    # Find and process all bounding boxes.
    s2_height, s2_width = s2_layer.shape[2:]
    print s2_height,s2_width
    bboxes = MakeBoundingBoxes(s2_height, s2_width, self.step_size,
        self.bbox_size)

    dvalues_per_bbox = list()
    start_time = time.time()
    p_labels = list()
    print "Number of bounding boxes:", len(bboxes)
    import progressbar
    pbar = progressbar.ProgressBar()

    for bbox in bboxes:
      # get a 4d array and take chunks out of that array then flatten for svm
      window = s2_layer[ :, :, bbox[0] : bbox[1], bbox[2] : bbox[3] ]
      predicted_label, dvalue = self.ClassifyC2Window(window)
     # print "Box and value", bbox, dvalue
      # Return the pre-thresholded decision value for this bounding box
      dvalues_per_bbox.append(dvalue)
      p_labels.append(predicted_label)
    end_time = time.time()
    logging.info("Time to process scale (%d boxes): %.2f secs" % (len(bboxes),
        end_time - start_time))

    return bboxes, np.array(dvalues_per_bbox),p_labels

  def Process(self, image):
    """Choose and classify bounding boxes for the given image.
    image -- (Image) input image
    RETURN (float list) scaling ratios, (3D list) bounding box for each region
           in scaled C1 coordinates, (list of np.ndarray) decision value for
           each region
    """
    image_scales = self.ChooseImageScales(image.size)
    logging.info("Image scales: %s", (image_scales,))
    bboxes_per_scale = list()
    dvalues_per_scale = list()
    results = [ self.ProcessScale(ScaleImage(image, scale))
        for scale in image_scales ]
    bboxes_per_scale, dvalues_per_scale, p_labels = zip(*results)
    logging.info("There are %d results" % sum(map(len, dvalues_per_scale)))
    return image_scales, bboxes_per_scale, dvalues_per_scale

"""
 calculate a gaussian value given a x,y point from -r to r
"""
def calc_gauss(x,y,r):
  pi = math.pi
  sigma = 1.2
  scale = 2*math.pi*((sigma*r)**2)
  gauss = (float(1)/float(2*pi*math.pow(sigma*r,2)))*math.exp(-(float(math.pow(x,2)+math.pow(y,2))/float(2*math.pow(sigma*r,2))))
  return 1-(gauss*scale)

"""
build the neighborhood suppression matrix such that it is the size of the global max bounding box.
find the "radius" of that box, 1/2 the width, and fill a NxN matrix with the values of 1-calc_gause(x,y). These will be our suppression values!
"""

def build_neighborhood(bbox):

  box_length = (bbox[2]-bbox[0])
  box_length = box_length + int((box_length*.35))
  #check to see if the bbox we found was less than the average vehicle size found in the database or not
  #if box_length < 344:
  #  box_length = box_length*2
  
  r = int(box_length/2)
  n = 2*r+1

  # fill an nxn matrix with zeros
  neighborhood = np.zeros(shape=(n,n))

  for y in range(-(r),(r+1)):
    for x in range(-(r),(r+1)):
     y_ind = y + r
     x_ind = x + r
     
     gauss_val = calc_gauss(x,y,r)

     neighborhood[y_ind][x_ind] = gauss_val

  return neighborhood
"""
Helper function to find the center of a bounding box for neighborhood suppression
"""
def find_center(bbox):

  w = bbox[0]+bbox[2]
  h = bbox[1]+bbox[3]
  xc = w/2
  yc = h/2

  return xc,yc

"""
Draw the bounding box from a series of line objects.
"""
def draw_bbox(bbox,image,obj_type):
  fill_color = "red"

  if obj_type == 2:
    #color for the detection of a bike
    fill_color = "green"
  elif obj_type == 1:
    # color for the detection of a pedestrian
    fill_color = "blue"


  x0,y0,x1,y1 = bbox
  p1 = (x0,y0)
  p2 = (x1,y0)
  p3 = (x0,y1)
  p4 = (x1,y1)

  image_copy = image.copy();
  draw = ImageDraw.Draw(image_copy)
  draw.line([p1,p2],fill_color,width= 2)
  draw.line([p1,p3],fill_color,width=2)
  draw.line([p2,p4],fill_color,width = 2)
  draw.line([p3,p4],fill_color, width = 2)

  return image_copy

# Perform object detection and return a dictionary of possible detections
def main(xform_dir, image_path, step_size, bbox_size,debug = False):
  if debug:
    logging.getLogger().setLevel(logging.INFO)
  # list of class models from svm
  experimental_models = list()


  # load the experiment data for each classifer
  experimental_models.append(LoadExperiment(os.path.join(xform_dir, "exp.dat")))



  image = Image.open(image_path)

  # perform this chunk of code 3 times for EACH classifier.
  detection_results = list()

  for classifier in range(0,len(experimental_models)):
    #load the svm models for each classifier
    #svm_model = svm.ScaledSvm(classifier = exp.classifier, scaler = exp.scaler)
    #~ svm_model = svm.ScaledSvm(classifier = experimental_models[classifier].classifier, scaler = experimental_models[classifier].scaler)
    svm_model = experimental_models[classifier].classifier

    #set up windowers for each classifier, should probably try to find a better way to do this
    #windower = Windower(exp.model, svm_model, step_size, bbox_size, debug = debug)

    windower = Windower(experimental_models[classifier].model, svm_model, step_size, bbox_size, debug = debug)
    image_scales, bboxes_per_scale, dvalues_per_scale = windower.Process(image)

    # take the decision values and the boxes and map them to the image space and flatten into one list
    def flatten_layers(bboxes_per_scale,dvalues_per_scale,image_scales):
      bboxes = list()
      dvalues = list()
      for scale in range(0,len(bboxes_per_scale)):
        for box in range(0,len(bboxes_per_scale[scale])):

          # map the box back to image space and grab the dvalue of that box and scale
          bbox = bboxes_per_scale[scale][box]

          b = windower.MapC1RegionToImageBox(bbox,image_scales[scale])
          #print "Image map", b,bbox,dvalues_per_scale[scale][box]
          bboxes.append(b)
          dvalues.append(dvalues_per_scale[scale][box])
      return bboxes,dvalues


    """ suppress regions of the image using Local Neighborhood suppresion."""
    def suppress_neighborhood(bboxes,dvals):
      #print "Max box",bboxes[np.argmax(dvals)]

      """
      taking the global max bbox, suppress all bboxes within the max bounding box
      """
      global_max = bboxes[np.argmax(dvals)]


      global_center = find_center(global_max)

      neighborhood = build_neighborhood (global_max)

      x0 ,y0,x1,y1 = zip(global_max)

      gxc = global_center[0] - x0[0]
      gyc = global_center[1] - y0[0]
      
      decision_val = copy.deepcopy(dvals[np.argmax(dvals)][0])
      #print decision_val
      index = np.argmax(dvals)
      if decision_val <= 0.0 :
        return global_max,decision_val,-1
      
      dvals[np.argmax(dvals)][0] = dvals[np.argmax(dvals)][0] * 0

      for ind in range(0,len(bboxes)):
        tmp_box = zip(bboxes[ind])

        xp0,yp0,xp1,yp1 = tmp_box

        """
        Look to see if the center of the box is within the neighborhood by some pixel amount.
        If it is then suppress that crop in the neighborhood by figuring out where it falls within that matrix.
        """
        xc,yc = zip(find_center(bboxes[ind]))


        if xc[0] > x0[0] and xc[0] < x1[0] and yc[0] > y0[0] and yc[0] < y1[0]:
          xc_index = xc[0]-x0[0]
          yc_index = yc[0]-y0[0]
          dvals[ind][0] = dvals[ind][0]*neighborhood[yc_index-1][xc_index-1]

      return global_max,decision_val,index

    """ As a test, draw the bounding box for the second crop.
    scale = 1
    bbox = bboxes_per_scale[scale][1]
    scale = image_scales[scale]



    # Alternatively, we could search the image for the best match.
    max_scale = np.argmax(dvalues.max() for dvalues in dvalues_per_scale)

    max_region = dvalues_per_scale[max_scale].argmax()

    bbox = bboxes_per_scale[max_scale][max_region]
    scale = image_scales[max_scale]
    sanity = windower.MapC1RegionToImageBox(bbox,scale)
	"""
    #ShowBoundingBox(bbox, scale, "best-match")
    # flatten out the boxes with a tuple of box and dvalue??
    bboxes,dvals = flatten_layers(bboxes_per_scale,dvalues_per_scale,image_scales)
    print "saving off dval data"
    dvals_copy_file = open(os.path.join('pickledCrops','dvals.dat'),'wb')
    pickle.dump(dvals,dvals_copy_file)
    dvals_copy_file.close()
    print "Loading crops pickle..."
    dvals_copy = pickle.load(open(os.path.join('pickledCrops','dvals.dat'),'rb'))

    # run till we are left with no global max over 0
    #while dvals[np.argmax(dvals)][0] != 0.0:
    #for i in range(0,15):
    obj_count = 0
    print "Finding objects..."
    while True:
      
      box_draw,decision,ind = suppress_neighborhood(bboxes,dvals)
      
    
      print box_draw,decision
      if decision <= 0.0 or obj_count == 20 :
        print "No more potential objects."
        break
      detection_results.append((box_draw,decision,os.path.basename(image_path)))
      image = draw_bbox(box_draw,image,classifier)#ShowBoundingBox(box_draw)
      obj_count += 1
    image.save(os.path.join('../gdittmar/experimentResults/Windowing/DetectionC1/C2_20limitSecond',os.path.basename(image_path)+'.jpg'))

    #print len(detection_results)
    all_results = list()
    # grab each box dval pair and put them in a list as tuples
    for box in range(0,len(bboxes)):
      all_results.append((bboxes[box],dvals_copy[box],ind,os.path.basename(image_path)))
    print "Number of results", len(detection_results),len(all_results)
    return detection_results,all_results

""" implement s2 layer detection windowing

  Get X FOR S2 bounding box = n/8 of origional image size,
  BOUNDING BOX = (y0,y1,x0,x1)

  X = s2[:,:,y0:y1,x0:x1]

  -------------------------------------------
  Slice up s2 layer data:
  Axis of s2 correspond to (Scale,Protoype,y-off,x-off)
  Let Y be the c2 feature vector for neighborhood X of s2 activity.
  X' = np.rollaxis(X,1)
  X' array has axis (prototype,scale,y-off,x-off)
  X'' = np.reshape(num_protos,-1)
  where num_protos = len(X')
  Y = X''.argmax(1)

"""

if __name__ == '__main__':
  if len(sys.argv) < 3:
    #sys.exit("usage: %s XFORM-DIR IMAGE WINDOW-STEP-SIZE THRESHOLD" % sys.argv[0])
    sys.exit("usage: %s XFORM-DIR IMAGE WINDOW-STEP-SIZE" % sys.argv[0])
  xform_dir,image_path,step_size = sys.argv[1:4]
  # Use a bounding box size (in C1 coordinates) of 24 units, which is equivalent
  # to 128 pixels in image space.
  bbox_size = 9
  main(xform_dir,image_path,int(step_size),bbox_size,debug = True)
